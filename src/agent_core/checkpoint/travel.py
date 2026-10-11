"""时间旅行 checkpoint：版本管理与回滚。

职责：
- `CheckpointVersion`：单个 checkpoint 版本（state + metadata）的值对象；
- `TimeTravelCheckpointer`：多版本存储，支持保存/加载/回滚/对比/删除版本，
  每次保存创建新版本，可选 JSON 文件持久化，版本数量有上限。

依赖方向：本模块仅依赖 `naming`（文件名清洗与原子写入），不反向依赖 `store`。
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.checkpoint.naming import _atomic_write_json, _sanitize_thread_id
from agent_core.errors import CheckpointError

logger = logging.getLogger(__name__)


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
    5. 每个 thread 的版本数量有上限，超过后淘汰最旧版本，避免无限膨胀
    """

    def __init__(
        self,
        storage_dir: str | Path | None = None,
        *,
        max_versions_per_thread: int = 100,
    ) -> None:
        """初始化。

        Args:
            storage_dir: 存储目录（可选，None 表示仅内存存储）。
            max_versions_per_thread: 每个 thread 保留的最大版本数，
                追加超过上限时淘汰最旧版本（默认 100）。
        """
        if max_versions_per_thread < 1:
            raise ValueError("max_versions_per_thread 必须大于 0")
        self._storage_dir = Path(storage_dir) if storage_dir else None
        if self._storage_dir:
            self._storage_dir.mkdir(parents=True, exist_ok=True)

        # thread_id -> list of CheckpointVersion
        self._versions: dict[str, list[CheckpointVersion]] = {}

        # 当前版本索引
        self._current_version: dict[str, int] = {}

        # 每个 thread 的版本数量上限
        self._max_versions_per_thread = max_versions_per_thread

    def _get_version_file(self, thread_id: str) -> Path | None:
        """获取版本文件路径（文件名经 `_sanitize_thread_id` 严格清洗）。"""
        if not self._storage_dir:
            return None
        safe_id = _sanitize_thread_id(thread_id)
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
        # 创建版本
        version_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC).isoformat()

        version = CheckpointVersion(
            version_id=version_id,
            timestamp=timestamp,
            state=copy.deepcopy(state),
            metadata=metadata or {},
        )

        # 添加到版本列表（超过容量上限时淘汰最旧版本，指针随淘汰前移）
        if thread_id not in self._versions:
            self._versions[thread_id] = []
        versions = self._versions[thread_id]

        # 记录保存前的当前指针：持久化失败时按版本身份恢复（淘汰会改变索引）。
        previous_index = self._current_version.get(thread_id)
        previous_current: CheckpointVersion | None = None
        if previous_index is not None and 0 <= previous_index < len(versions):
            previous_current = versions[previous_index]

        versions.append(version)
        self._trim_versions(thread_id)
        self._current_version[thread_id] = len(versions) - 1

        # 持久化到文件
        try:
            await self._persist_versions(thread_id)
        except Exception:
            # 按身份移除本次追加的版本；并发 save_version 时列表末尾可能是其他调用者的版本。
            for position in range(len(versions) - 1, -1, -1):
                if versions[position] is version:
                    del versions[position]
                    break
            self._restore_current_pointer(thread_id, previous_current)
            raise

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
                # 当前版本指针也属于时间旅行状态；必须和版本列表一起持久化，
                # 否则进程重启后会再次指向最新版本。
                await self._persist_versions(thread_id)
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

    async def delete(self, thread_id: str) -> None:
        """删除一个会话的全部版本，并满足 Agent Loop 的 Checkpointer 协议。"""
        self._versions.pop(thread_id, None)
        self._current_version.pop(thread_id, None)
        file_path = self._get_version_file(thread_id)
        if file_path is not None:
            try:
                file_path.unlink(missing_ok=True)
            except OSError as exc:
                raise CheckpointError(f"删除会话 {thread_id!r} 的 checkpoint 失败：{exc}") from exc

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

        if state1 is None or state2 is None:
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

    def _trim_versions(self, thread_id: str) -> int:
        """把版本数量收敛到 `_max_versions_per_thread` 内，返回淘汰的最旧版本数。

        淘汰只从列表头部（最旧）进行；当前指针所指版本被淘汰时，指针前移到
        它的继任版本（最旧存活版本），未被淘汰时指针仍指向同一版本。
        """
        versions = self._versions.get(thread_id)
        if not versions:
            return 0
        excess = len(versions) - self._max_versions_per_thread
        if excess <= 0:
            return 0
        del versions[:excess]
        current = self._current_version.get(thread_id)
        if current is not None:
            self._current_version[thread_id] = max(current - excess, 0)
        logger.info(
            "Evicted %d oldest checkpoint versions for thread %r (limit: %d)",
            excess,
            thread_id,
            self._max_versions_per_thread,
        )
        return excess

    def _restore_current_pointer(self, thread_id: str, previous: CheckpointVersion | None) -> None:
        """按版本身份恢复保存前的当前指针（容量淘汰会改变索引）。"""
        versions = self._versions.get(thread_id, [])
        if previous is None:
            self._current_version.pop(thread_id, None)
            return
        position = next((i for i, item in enumerate(versions) if item is previous), None)
        if position is not None:
            self._current_version[thread_id] = position
        elif versions:
            # 保存前的指针版本已被容量淘汰：指针前移到最旧存活版本。
            self._current_version[thread_id] = 0
        else:
            self._current_version.pop(thread_id, None)

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
            _atomic_write_json(file_path, data)
        except Exception as exc:
            raise CheckpointError(f"持久化会话 {thread_id!r} 的 checkpoint 版本失败：{exc}") from exc

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
            # 历史文件中的版本数可能超过当前上限：加载后同样收敛，指针随淘汰前移。
            self._trim_versions(thread_id)
        except Exception as exc:
            raise CheckpointError(f"加载会话 {thread_id!r} 的 checkpoint 版本失败：{exc}") from exc
