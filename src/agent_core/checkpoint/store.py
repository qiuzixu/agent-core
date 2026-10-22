"""Checkpoint 契约层：协议定义 + 内存实现。

本模块是 checkpoint 包的入口契约：

- `Checkpointer` 协议：Agent Loop 所需的最小存储接口（save / load / delete），
  通过 `checkpoint_ns` 支持 (thread_id, checkpoint_ns) 二级隔离——
  对话层用空字符串，工作流层用 "workflow:<execution_id>"；
- `MemoryCheckpointer`：进程内字典实现，每个 (thread_id, ns) 只保留最新快照，
  无版本历史，进程退出即丢。适用于测试环境（快、隔离）。

后端选择速查（详见 factory.py）：
- 测试：MemoryCheckpointer（本模块）
- 开发：SqliteCheckpointer（sqlite.py，落盘 checkpoints.db）
- 生产：PostgresCheckpointer（postgres.py，多实例共享）
- 版本历史 / 回滚 / 分支：TimeTravelCheckpointer（travel.py）

包内职责划分：
- `naming`：文件名清洗与原子写入（无依赖的底层工具）；
- `store`（本模块）：协议 + 内存当前快照实现；
- `travel`：CheckpointVersion 与时间旅行多版本存储；
- `schema`：schema 版本戳与链式迁移。
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

    分隔符用 NUL（\x00）：假设 thread_id 本身不含控制字符，
    因此拼接后的 key 可以无歧义地还原出 (thread_id, ns) 二元组。
    ns 为空（对话层）时直接返回 thread_id，保持与旧版存储兼容。
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
        # deepcopy 隔离：调用方后续修改 state 不会影响已存快照；
        # load() 返回的也是副本，避免外部改动破坏内部存储。
        # metadata 仅供版本型实现使用，内存当前快照只保留 state。
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


