"""手写 checkpoint 持久化。

功能：
- 保存和加载会话状态（state）
- 支持按 thread_id 隔离不同会话
- 简化实现：内存存储 + 可选的 JSON 文件持久化
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Protocol

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
        if state:
            logger.debug("Loaded checkpoint for thread %r (keys: %d)", thread_id, len(state))
        return copy.deepcopy(state) if state else None

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
        """获取 thread 对应的文件路径。"""
        # 简单的文件名清洗（实际项目应该更严格）
        safe_id = thread_id.replace("/", "_").replace("\\", "_")
        return self._storage_dir / f"{safe_id}.json"

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
            with file_path.open("w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
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
                state = json.load(f)
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
        """列出所有会话 ID（从文件名提取）。"""
        threads = []
        for file_path in self._storage_dir.glob("*.json"):
            # 反向清洗文件名得到 thread_id
            thread_id = file_path.stem
            threads.append(thread_id)
        return threads


# ──────────────────────────────────────────────
# 时间旅行功能（Checkpoint 版本管理）
# ──────────────────────────────────────────────


class CheckpointVersion:
    """Checkpoint 版本信息。"""

    def __init__(
        self,
        version_id: str,
        timestamp: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """初始化。

        Args:
            version_id: 版本 ID。
            timestamp: 时间戳（ISO 格式）。
            state: 状态字典。
            metadata: 元数据（可选）。
        """
        self.version_id = version_id
        self.timestamp = timestamp
        self.state = state
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "version_id": self.version_id,
            "timestamp": self.timestamp,
            "state": self.state,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckpointVersion:
        """从字典创建。"""
        return cls(
            version_id=data["version_id"],
            timestamp=data["timestamp"],
            state=data["state"],
            metadata=data.get("metadata"),
        )


class TimeTravelCheckpointer:
    """支持时间旅行的 Checkpointer。

    功能：
    1. 保存多个版本（每次保存创建新版本）
    2. 查看历史版本列表
    3. 回滚到任意版本
    4. 对比两个版本
    """

    def __init__(self, storage_dir: str | Path | None = None) -> None:
        """初始化。

        Args:
            storage_dir: 存储目录（可选，None 表示仅内存存储）。
        """
        self._storage_dir = Path(storage_dir) if storage_dir else None
        if self._storage_dir:
            self._storage_dir.mkdir(parents=True, exist_ok=True)

        # thread_id -> list of CheckpointVersion
        self._versions: dict[str, list[CheckpointVersion]] = {}

        # 当前版本索引
        self._current_version: dict[str, int] = {}

    def _get_version_file(self, thread_id: str) -> Path | None:
        """获取版本文件路径。"""
        if not self._storage_dir:
            return None
        safe_id = thread_id.replace("/", "_").replace("\\", "_")
        return self._storage_dir / f"{safe_id}_versions.json"

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """保存新版本。

        Args:
            thread_id: 会话 ID。
            state: 状态字典。
            metadata: 元数据（可选）。

        Returns:
            版本 ID。
        """
        import datetime
        import uuid

        # 创建版本
        version_id = str(uuid.uuid4())[:8]
        timestamp = datetime.datetime.now().isoformat()

        version = CheckpointVersion(
            version_id=version_id,
            timestamp=timestamp,
            state=copy.deepcopy(state),
            metadata=metadata or {},
        )

        # 添加到版本列表
        if thread_id not in self._versions:
            self._versions[thread_id] = []

        self._versions[thread_id].append(version)
        self._current_version[thread_id] = len(self._versions[thread_id]) - 1

        # 持久化到文件
        await self._persist_versions(thread_id)

        logger.info(
            "Saved checkpoint version %s for thread %r (total versions: %d)",
            version_id,
            thread_id,
            len(self._versions[thread_id]),
        )

        return version_id

    async def load(self, thread_id: str, version_id: str | None = None) -> dict[str, Any] | None:
        """加载指定版本的 state。

        Args:
            thread_id: 会话 ID。
            version_id: 版本 ID（None 表示最新版本）。

        Returns:
            状态字典，如果不存在则返回 None。
        """
        # 从文件加载（如果需要）
        await self._load_versions_from_file(thread_id)

        versions = self._versions.get(thread_id, [])
        if not versions:
            return None

        # 查找版本
        if version_id:
            for v in versions:
                if v.version_id == version_id:
                    logger.info(
                        "Loaded checkpoint version %s for thread %r",
                        version_id,
                        thread_id,
                    )
                    return copy.deepcopy(v.state)
            logger.warning(
                "Checkpoint version %s not found for thread %r",
                version_id,
                thread_id,
            )
            return None

        # 没有显式版本时读取当前指针。回滚后不能意外跳回最新版本。
        current_index = self._current_version.get(thread_id, len(versions) - 1)
        current_index = max(0, min(current_index, len(versions) - 1))
        latest = versions[current_index]
        logger.info(
            "Loaded current checkpoint version %s for thread %r",
            latest.version_id,
            thread_id,
        )
        return copy.deepcopy(latest.state)

    async def list_versions(self, thread_id: str) -> list[dict[str, Any]]:
        """列出所有版本。

        Args:
            thread_id: 会话 ID。

        Returns:
            版本信息列表（每个元素包含 version_id, timestamp, metadata）。
        """
        await self._load_versions_from_file(thread_id)

        versions = self._versions.get(thread_id, [])
        return [
            {
                "version_id": v.version_id,
                "timestamp": v.timestamp,
                "metadata": v.metadata,
            }
            for v in versions
        ]

    async def rollback(self, thread_id: str, version_id: str) -> bool:
        """回滚到指定版本。

        注意：这不会删除后续版本，只是将当前指针移动到指定版本。

        Args:
            thread_id: 会话 ID。
            version_id: 目标版本 ID。

        Returns:
            是否成功回滚。
        """
        await self._load_versions_from_file(thread_id)

        versions = self._versions.get(thread_id, [])
        if not versions:
            logger.warning("No versions found for thread %r", thread_id)
            return False

        # 查找版本索引
        for i, v in enumerate(versions):
            if v.version_id == version_id:
                self._current_version[thread_id] = i
                logger.info(
                    "Rolled back to version %s for thread %r (index: %d)",
                    version_id,
                    thread_id,
                    i,
                )
                return True

        logger.warning(
            "Version %s not found for thread %r",
            version_id,
            thread_id,
        )
        return False

    async def delete_version(self, thread_id: str, version_id: str) -> bool:
        """删除指定版本。

        Args:
            thread_id: 会话 ID。
            version_id: 版本 ID。

        Returns:
            是否成功删除。
        """
        await self._load_versions_from_file(thread_id)

        versions = self._versions.get(thread_id, [])
        if not versions:
            return False

        # 查找并删除
        for i, v in enumerate(versions):
            if v.version_id == version_id:
                versions.pop(i)
                if versions:
                    current = self._current_version.get(thread_id, len(versions) - 1)
                    if i < current:
                        current -= 1
                    self._current_version[thread_id] = max(0, min(current, len(versions) - 1))
                else:
                    self._current_version.pop(thread_id, None)
                await self._persist_versions(thread_id)
                logger.info(
                    "Deleted version %s for thread %r",
                    version_id,
                    thread_id,
                )
                return True

        return False

    async def compare_versions(
        self,
        thread_id: str,
        version_id_1: str,
        version_id_2: str,
    ) -> dict[str, Any]:
        """对比两个版本。

        Args:
            thread_id: 会话 ID。
            version_id_1: 版本 1 ID。
            version_id_2: 版本 2 ID。

        Returns:
            对比结果（包含差异信息）。
        """
        state1 = await self.load(thread_id, version_id_1)
        state2 = await self.load(thread_id, version_id_2)

        if not state1 or not state2:
            return {"error": "One or both versions not found"}

        # 简单对比（实际项目可以使用更复杂的 diff 算法）
        diff = {
            "version_1": version_id_1,
            "version_2": version_id_2,
            "keys_only_in_v1": list(set(state1.keys()) - set(state2.keys())),
            "keys_only_in_v2": list(set(state2.keys()) - set(state1.keys())),
            "common_keys": list(set(state1.keys()) & set(state2.keys())),
        }

        return diff

    async def _persist_versions(self, thread_id: str) -> None:
        """持久化版本列表到文件。"""
        if not self._storage_dir:
            return

        file_path = self._get_version_file(thread_id)
        if not file_path:
            return

        versions = self._versions.get(thread_id, [])
        data = {
            "thread_id": thread_id,
            "versions": [v.to_dict() for v in versions],
            "current_version": self._current_version.get(thread_id, 0),
        }

        try:
            with file_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("Failed to persist versions: %s", exc)

    async def _load_versions_from_file(self, thread_id: str) -> None:
        """从文件加载版本列表。"""
        if thread_id in self._versions:
            return  # 已加载

        if not self._storage_dir:
            return

        file_path = self._get_version_file(thread_id)
        if not file_path or not file_path.exists():
            return

        try:
            with file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            self._versions[thread_id] = [CheckpointVersion.from_dict(v) for v in data.get("versions", [])]
            self._current_version[thread_id] = data.get("current_version", 0)
        except Exception as exc:
            logger.error("Failed to load versions from file: %s", exc)
