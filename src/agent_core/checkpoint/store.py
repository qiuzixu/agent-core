"""手写 checkpoint 持久化。

功能：
- 保存和加载会话状态（state）
- 支持按 thread_id + checkpoint_ns 隔离不同会话/工作流层
- 简化实现：内存存储 + 可选的 JSON 文件持久化

包内职责划分：
- `naming`：文件名清洗与原子写入（无依赖的底层工具）；
- 本模块（`store`）：当前快照的 `Checkpointer` 协议与内存/文件两个实现；
- `travel`：`CheckpointVersion` 与时间旅行多版本存储。
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from agent_core.checkpoint.naming import _atomic_write_json, _restore_thread_id, _sanitize_thread_id
from agent_core.errors import CheckpointError

logger = logging.getLogger(__name__)

# 对话层默认 namespace（空字符串）
DEFAULT_CHECKPOINT_NS = ""


def _storage_key(thread_id: str, checkpoint_ns: str) -> str:
    """把 (thread_id, checkpoint_ns) 组合成内部存储 key。

    同一 thread_id 下不同 namespace 的数据互不覆盖。
    """
    if not checkpoint_ns or checkpoint_ns == DEFAULT_CHECKPOINT_NS:
        return thread_id
    return f"{thread_id}\x00{checkpoint_ns}"


class Checkpointer(Protocol):
    """Agent Loop 所需的最小 checkpoint 存储协议。

    具体实现可以是内存、文件、SQLite 或 PostgreSQL；Core 只依赖这个协议，
    不把数据库驱动和业务项目耦合进来。
    checkpoint_ns 用于在同一 thread_id 下隔离不同层（对话层为空，工作流层为 "workflow:<id>"）。
    """

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> str | None:
        """保存一个会话快照，返回实现生成的版本 ID（如有）。"""
        ...

    async def load(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> dict[str, Any] | None:
        """读取会话当前快照。"""
        ...

    async def delete(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
        """删除会话当前快照。"""
        ...


class MemoryCheckpointer:
    """内存 checkpoint 存储（进程内有效）。"""

    def __init__(self) -> None:
        # (thread_id, ns) composite key -> state
        self._storage: dict[str, dict[str, Any]] = {}

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
        """保存会话状态。

        Args:
            thread_id: 会话 ID。
            state: 状态字典。
            checkpoint_ns: checkpoint 命名空间（对话层为空，工作流层为 "workflow:<id>"）。
        """
        # metadata 由版本型实现使用；内存当前快照保持原有 state 结构不变。
        del metadata
        key = _storage_key(thread_id, checkpoint_ns)
        self._storage[key] = copy.deepcopy(state)
        logger.debug("Saved checkpoint for thread %r ns %r (keys: %d)", thread_id, checkpoint_ns, len(state))

    async def load(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> dict[str, Any] | None:
        """加载会话状态。"""
        key = _storage_key(thread_id, checkpoint_ns)
        state = self._storage.get(key)
        if state is not None:
            logger.debug("Loaded checkpoint for thread %r ns %r (keys: %d)", thread_id, checkpoint_ns, len(state))
        return copy.deepcopy(state) if state is not None else None

    async def delete(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
        """删除会话状态。"""
        key = _storage_key(thread_id, checkpoint_ns)
        if key in self._storage:
            del self._storage[key]
            logger.debug("Deleted checkpoint for thread %r ns %r", thread_id, checkpoint_ns)

    def list_threads(self) -> list[str]:
        """列出所有会话 ID（去重，不区分 namespace）。"""
        return sorted({key.split("\x00")[0] for key in self._storage})

    def list_namespaces(self, thread_id: str) -> list[str]:
        """列出该 thread_id 下所有已使用的 checkpoint namespace。"""
        prefix = f"{thread_id}\x00"
        namespaces = [
            key[len(prefix):]
            for key in self._storage
            if key.startswith(prefix)
        ]
        # 无 ns 的 key（纯 thread_id）也算对话层
        if thread_id in self._storage:
            namespaces.insert(0, DEFAULT_CHECKPOINT_NS)
        return sorted(set(namespaces))

    async def list_versions(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> list[dict[str, Any]]:
        """返回当前快照作为单版本列表（内存实现无历史版本）。"""
        key = _storage_key(thread_id, checkpoint_ns)
        state = self._storage.get(key)
        if state is None:
            return []
        return [{"version_id": "current", "timestamp": "", "metadata": {}}]


class FileCheckpointer:
    """文件系统 checkpoint 存储（JSON 格式）。

    每个 (thread_id, checkpoint_ns) 存储为独立的 JSON 文件。
    ns 为空时文件名与旧版一致（向后兼容）；ns 非空时文件名带 ns 哈希后缀。
    """

    def __init__(self, storage_dir: str | Path) -> None:
        """
        Args:
            storage_dir: 存储目录。
        """
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, thread_id: str, checkpoint_ns: str = DEFAULT_CHECKPOINT_NS) -> Path:
        """获取 (thread_id, ns) 对应的文件路径。

        ns 为空时文件名与旧版完全一致（向后兼容）。
        ns 非空时在文件名末尾追加 __ns_<sanitized_ns>。
        """
        safe_id = _sanitize_thread_id(thread_id)
        if not checkpoint_ns or checkpoint_ns == DEFAULT_CHECKPOINT_NS:
            return self._storage_dir / f"{safe_id}.json"
        safe_ns = _sanitize_thread_id(checkpoint_ns)
        return self._storage_dir / f"{safe_id}__ns_{safe_ns}.json"

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
        """保存会话状态到文件。"""
        del metadata
        file_path = self._get_file_path(thread_id, checkpoint_ns)
        try:
            _atomic_write_json(file_path, state)
            logger.debug("Saved checkpoint for thread %r ns %r to %s", thread_id, checkpoint_ns, file_path)
        except Exception as exc:
            raise CheckpointError(f"Failed to save checkpoint for thread {thread_id!r} ns {checkpoint_ns!r}: {exc}") from exc

    async def load(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> dict[str, Any] | None:
        """从文件加载会话状态。"""
        file_path = self._get_file_path(thread_id, checkpoint_ns)
        if not file_path.exists():
            return None

        try:
            with file_path.open("r", encoding="utf-8") as f:
                raw_state = json.load(f)
            if not isinstance(raw_state, dict):
                raise CheckpointError(f"Checkpoint {thread_id!r} ns {checkpoint_ns!r} 的根节点必须是对象")
            state: dict[str, Any] = {str(key): value for key, value in raw_state.items()}
            logger.debug("Loaded checkpoint for thread %r ns %r", thread_id, checkpoint_ns)
            return state
        except Exception as exc:
            raise CheckpointError(f"Failed to load checkpoint for thread {thread_id!r} ns {checkpoint_ns!r}: {exc}") from exc

    async def delete(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
        """删除会话状态文件。"""
        file_path = self._get_file_path(thread_id, checkpoint_ns)
        if file_path.exists():
            file_path.unlink()
            logger.debug("Deleted checkpoint for thread %r ns %r at %s", thread_id, checkpoint_ns, file_path)

    def list_threads(self) -> list[str]:
        """列出所有会话 ID（去重，不区分 namespace；还原文件名中的 ns 后缀）。"""
        seen: set[str] = set()
        for file_path in self._storage_dir.glob("*.json"):
            stem = file_path.stem
            # 去掉 __ns_<sanitized> 后缀还原 thread_id
            ns_sep = stem.find("__ns_")
            raw_id = stem[:ns_sep] if ns_sep != -1 else stem
            seen.add(_restore_thread_id(raw_id))
        return sorted(seen)

    def list_namespaces(self, thread_id: str) -> list[str]:
        """列出该 thread_id 下所有已使用的 checkpoint namespace。"""
        safe_id = _sanitize_thread_id(thread_id)
        base = f"{safe_id}.json"
        prefix = f"{safe_id}__ns_"
        namespaces: set[str] = set()
        for file_path in self._storage_dir.glob("*.json"):
            name = file_path.name
            if name == base:
                namespaces.add(DEFAULT_CHECKPOINT_NS)
            elif name.startswith(prefix):
                raw_ns = name[len(prefix):-len(".json")]
                namespaces.add(_restore_thread_id(raw_ns))
        return sorted(namespaces)

    async def list_versions(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> list[dict[str, Any]]:
        """返回当前快照作为单版本列表（文件实现无历史版本）。"""
        state = await self.load(thread_id, checkpoint_ns=checkpoint_ns)
        if state is None:
            return []
        return [{"version_id": "current", "timestamp": "", "metadata": {}}]
