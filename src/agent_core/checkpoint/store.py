"""手写 checkpoint 持久化。

功能：
- 保存和加载会话状态（state）
- 支持按 thread_id 隔离不同会话
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


class Checkpointer(Protocol):
    """Agent Loop 所需的最小 checkpoint 存储协议。

    具体实现可以是内存、文件、SQLite 或 PostgreSQL；Core 只依赖这个协议，
    不把数据库驱动和业务项目耦合进来。
    """

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """保存一个会话快照，返回实现生成的版本 ID（如有）。"""
        ...

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        """读取会话当前快照。"""
        ...

    async def delete(self, thread_id: str) -> None:
        """删除会话当前快照。"""
        ...


class MemoryCheckpointer:
    """内存 checkpoint 存储（进程内有效）。"""

    def __init__(self) -> None:
        # thread_id -> state
        self._storage: dict[str, dict[str, Any]] = {}

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """保存会话状态。

        Args:
            thread_id: 会话 ID。
            state: 状态字典。
        """
        # metadata 由版本型实现使用；内存当前快照保持原有 state 结构不变。
        del metadata
        self._storage[thread_id] = copy.deepcopy(state)
        logger.debug("Saved checkpoint for thread %r (keys: %d)", thread_id, len(state))

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        """加载会话状态。

        Args:
            thread_id: 会话 ID。

        Returns:
            状态字典，如果不存在则返回 None。
        """
        state = self._storage.get(thread_id)
        if state is not None:
            logger.debug("Loaded checkpoint for thread %r (keys: %d)", thread_id, len(state))
        return copy.deepcopy(state) if state is not None else None

    async def delete(self, thread_id: str) -> None:
        """删除会话状态。

        Args:
            thread_id: 会话 ID。
        """
        if thread_id in self._storage:
            del self._storage[thread_id]
            logger.debug("Deleted checkpoint for thread %r", thread_id)

    def list_threads(self) -> list[str]:
        """列出所有会话 ID。"""
        return list(self._storage.keys())


class FileCheckpointer:
    """文件系统 checkpoint 存储（JSON 格式）。

    每个 thread 存储为独立的 JSON 文件。
    """

    def __init__(self, storage_dir: str | Path) -> None:
        """
        Args:
            storage_dir: 存储目录路径。
        """
        self._storage_dir = Path(storage_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, thread_id: str) -> Path:
        """获取 thread 对应的文件路径。

        文件名经 `_sanitize_thread_id` 严格清洗：单射且可逆，含 ``/``、``\\``、
        ``:`` 等 Windows 非法字符的 thread_id 不会互相覆盖。
        """
        return self._storage_dir / f"{_sanitize_thread_id(thread_id)}.json"

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """保存会话状态到文件。

        Args:
            thread_id: 会话 ID。
            state: 状态字典。
        """
        # 文件快照接口与内存实现保持一致，metadata 由需要版本历史的实现使用。
        del metadata
        file_path = self._get_file_path(thread_id)
        try:
            _atomic_write_json(file_path, state)
            logger.debug("Saved checkpoint for thread %r to %s", thread_id, file_path)
        except Exception as exc:
            raise CheckpointError(f"Failed to save checkpoint for thread {thread_id!r}: {exc}") from exc

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        """从文件加载会话状态。

        Args:
            thread_id: 会话 ID。

        Returns:
            状态字典，如果文件不存在则返回 None。
        """
        file_path = self._get_file_path(thread_id)
        if not file_path.exists():
            return None

        try:
            with file_path.open("r", encoding="utf-8") as f:
                raw_state = json.load(f)
            if not isinstance(raw_state, dict):
                raise CheckpointError(f"Checkpoint {thread_id!r} 的根节点必须是对象")
            state: dict[str, Any] = {str(key): value for key, value in raw_state.items()}
            logger.debug("Loaded checkpoint for thread %r from %s", thread_id, file_path)
            return state
        except Exception as exc:
            raise CheckpointError(f"Failed to load checkpoint for thread {thread_id!r}: {exc}") from exc

    async def delete(self, thread_id: str) -> None:
        """删除会话状态文件。

        Args:
            thread_id: 会话 ID。
        """
        file_path = self._get_file_path(thread_id)
        if file_path.exists():
            file_path.unlink()
            logger.debug("Deleted checkpoint for thread %r at %s", thread_id, file_path)

    def list_threads(self) -> list[str]:
        """列出所有会话 ID（文件名主干经 `_restore_thread_id` 逆映射还原）。"""
        return [_restore_thread_id(file_path.stem) for file_path in self._storage_dir.glob("*.json")]
