"""时间旅行 checkpoint：版本管理与回滚。

职责：
- `CheckpointVersion`：单个 checkpoint 版本（state + metadata + namespace）的值对象；
- `TimeTravelCheckpointer`：多版本存储，支持保存/加载/回滚/对比/删除版本，
  每次保存创建新版本，可选 JSON 文件持久化，版本数量有上限。
- 支持按 checkpoint_ns 在同一 thread_id 下隔离不同层（对话层为空，工作流层为 "workflow:<id>"）。

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

from agent_core.checkpoint.naming import _atomic_write_json, _restore_thread_id, _sanitize_thread_id
from agent_core.errors import CheckpointError

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_NS = ""


def _storage_key(thread_id: str, checkpoint_ns: str) -> str:
    """把 (thread_id, checkpoint_ns) 组合成内部存储 key。"""
    if not checkpoint_ns or checkpoint_ns == DEFAULT_CHECKPOINT_NS:
        return thread_id
    return f"{thread_id}\x00{checkpoint_ns}"


class CheckpointVersion:
    """Checkpoint 版本信息。"""

    def __init__(
        self,
        version_id: str,
        timestamp: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
        """初始化。

        Args:
            version_id: 版本 ID。
            timestamp: 时间戳（ISO 格式）。
            state: 状态字典。
            metadata: 元数据（可选）。
            checkpoint_ns: checkpoint 命名空间（对话层为空，工作流层为 "workflow:<id>"）。
        """
        self.version_id = version_id
        self.timestamp = timestamp
        self.state = state
        self.metadata = metadata or {}
        self.checkpoint_ns = checkpoint_ns

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "version_id": self.version_id,
            "timestamp": self.timestamp,
            "state": self.state,
            "metadata": self.metadata,
            "checkpoint_ns": self.checkpoint_ns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckpointVersion:
        """从字典创建（兼容旧数据：无 checkpoint_ns 字段时默认对话层）。"""
        return cls(
            version_id=data["version_id"],
            timestamp=data["timestamp"],
            state=data["state"],
            metadata=data.get("metadata"),
            checkpoint_ns=data.get("checkpoint_ns", DEFAULT_CHECKPOINT_NS),
        )


class TimeTravelCheckpointer:
    """支持时间旅行的 Checkpointer。

    功能：
    1. 保存多个版本（每次保存创建新版本）
    2. 查看历史版本列表
    3. 回滚到任意版本
    4. 对比两个版本
    5. 每个 (thread_id, checkpoint_ns) 的版本数量有上限，超过后淘汰最旧版本
    6. 同一 thread_id 下不同 namespace 的数据互不覆盖
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
            max_versions_per_thread: 每个 (thread_id, ns) 保留的最大版本数。
        """
        if max_versions_per_thread < 1:
            raise ValueError("max_versions_per_thread 必须大于 0")
        self._storage_dir = Path(storage_dir) if storage_dir else None
        if self._storage_dir:
            self._storage_dir.mkdir(parents=True, exist_ok=True)

        # _storage_key(thread_id, ns) -> list of CheckpointVersion
        self._versions: dict[str, list[CheckpointVersion]] = {}

        # 当前版本索引
        self._current_version: dict[str, int] = {}

        # 每个 (thread_id, ns) 的版本数量上限
        self._max_versions_per_thread = max_versions_per_thread

    def _get_version_file(self, thread_id: str, checkpoint_ns: str = DEFAULT_CHECKPOINT_NS) -> Path | None:
        """获取版本文件路径。

        ns 为空时文件名与旧版一致（向后兼容）；ns 非空时带 __ns_<sanitized> 前缀。
        """
        if not self._storage_dir:
            return None
        safe_id = _sanitize_thread_id(thread_id)
        if not checkpoint_ns or checkpoint_ns == DEFAULT_CHECKPOINT_NS:
            return self._storage_dir / f"{safe_id}_versions.json"
        safe_ns = _sanitize_thread_id(checkpoint_ns)
        return self._storage_dir / f"{safe_id}__ns_{safe_ns}_versions.json"

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> str:
        """保存新版本。

        Args:
            thread_id: 会话 ID。
            state: 状态字典。
            metadata: 元数据（可选）。
            checkpoint_ns: checkpoint 命名空间。

        Returns:
            版本 ID。
        """
        key = _storage_key(thread_id, checkpoint_ns)
        version_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC).isoformat()

        version = CheckpointVersion(
            version_id=version_id,
            timestamp=timestamp,
            state=copy.deepcopy(state),
            metadata=metadata or {},
            checkpoint_ns=checkpoint_ns,
        )

        # 添加到版本列表（超过容量上限时淘汰最旧版本，指针随淘汰前移）
        if key not in self._versions:
            self._versions[key] = []
        versions = self._versions[key]

        # 记录保存前的当前指针：持久化失败时按版本身份恢复（淘汰会改变索引）。
        previous_index = self._current_version.get(key)
        previous_current: CheckpointVersion | None = None
        if previous_index is not None and 0 <= previous_index < len(versions):
            previous_current = versions[previous_index]

        versions.append(version)
        self._trim_versions(key)
        self._current_version[key] = len(versions) - 1

        # 持久化到文件
        try:
            await self._persist_versions(key)
        except Exception:
            # 按身份移除本次追加的版本；并发 save_version 时列表末尾可能是其他调用者的版本。
            for position in range(len(versions) - 1, -1, -1):
                if versions[position] is version:
                    del versions[position]
                    break
            self._restore_current_pointer(key, previous_current)
            raise

        logger.info(
            "Saved checkpoint version %s for thread %r ns %r (total versions: %d)",
            version_id,
            thread_id,
            checkpoint_ns,
            len(self._versions[key]),
        )

        return version_id

    async def load(
        self,
        thread_id: str,
        version_id: str | None = None,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> dict[str, Any] | None:
        """加载指定版本的 state。

        Args:
            thread_id: 会话 ID。
            version_id: 版本 ID（None 表示最新版本）。
            checkpoint_ns: checkpoint 命名空间。

        Returns:
            状态字典，如果不存在则返回 None。
        """
        key = _storage_key(thread_id, checkpoint_ns)
        await self._load_versions_from_file(key)

        versions = self._versions.get(key, [])
        if not versions:
            return None

        # 查找版本
        if version_id:
            for v in versions:
                if v.version_id == version_id:
                    logger.info(
                        "Loaded checkpoint version %s for thread %r ns %r",
                        version_id,
                        thread_id,
                        checkpoint_ns,
                    )
                    return copy.deepcopy(v.state)
            logger.warning(
                "Checkpoint version %s not found for thread %r ns %r",
                version_id,
                thread_id,
                checkpoint_ns,
            )
            return None

        # 没有显式版本时读取当前指针。回滚后不能意外跳回最新版本。
        current_index = self._current_version.get(key, len(versions) - 1)
        current_index = max(0, min(current_index, len(versions) - 1))
        latest = versions[current_index]
        logger.info(
            "Loaded current checkpoint version %s for thread %r ns %r",
            latest.version_id,
            thread_id,
            checkpoint_ns,
        )
        return copy.deepcopy(latest.state)

    async def list_versions(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> list[dict[str, Any]]:
        """列出所有版本。

        Args:
            thread_id: 会话 ID。
            checkpoint_ns: checkpoint 命名空间。

        Returns:
            版本信息列表（每个元素包含 version_id, timestamp, metadata）。
        """
        key = _storage_key(thread_id, checkpoint_ns)
        await self._load_versions_from_file(key)

        versions = self._versions.get(key, [])
        return [
            {
                "version_id": v.version_id,
                "timestamp": v.timestamp,
                "metadata": v.metadata,
            }
            for v in versions
        ]

    def list_namespaces(self, thread_id: str) -> list[str]:
        """列出该 thread_id 下所有已使用的 checkpoint namespace（仅内存中已加载的）。"""
        prefix = f"{thread_id}\x00"
        namespaces: set[str] = set()
        if thread_id in self._versions:
            namespaces.add(DEFAULT_CHECKPOINT_NS)
        for key in self._versions:
            if key.startswith(prefix):
                namespaces.add(key[len(prefix):])
        return sorted(namespaces)

    async def rollback(
        self,
        thread_id: str,
        version_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> bool:
        """回滚到指定版本。

        注意：这不会删除后续版本，只是将当前指针移动到指定版本。

        Args:
            thread_id: 会话 ID。
            version_id: 目标版本 ID。
            checkpoint_ns: checkpoint 命名空间。

        Returns:
            是否成功回滚。
        """
        key = _storage_key(thread_id, checkpoint_ns)
        await self._load_versions_from_file(key)

        versions = self._versions.get(key, [])
        if not versions:
            logger.warning("No versions found for thread %r ns %r", thread_id, checkpoint_ns)
            return False

        # 查找版本索引
        for i, v in enumerate(versions):
            if v.version_id == version_id:
                self._current_version[key] = i
                # 当前版本指针也属于时间旅行状态；必须和版本列表一起持久化，
                # 否则进程重启后会再次指向最新版本。
                await self._persist_versions(key)
                logger.info(
                    "Rolled back to version %s for thread %r ns %r (index: %d)",
                    version_id,
                    thread_id,
                    checkpoint_ns,
                    i,
                )
                return True

        logger.warning(
            "Version %s not found for thread %r ns %r",
            version_id,
            thread_id,
            checkpoint_ns,
        )
        return False

    async def delete_version(
        self,
        thread_id: str,
        version_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> bool:
        """删除指定版本。

        Args:
            thread_id: 会话 ID。
            version_id: 版本 ID。
            checkpoint_ns: checkpoint 命名空间。

        Returns:
            是否成功删除。
        """
        key = _storage_key(thread_id, checkpoint_ns)
        await self._load_versions_from_file(key)

        versions = self._versions.get(key, [])
        if not versions:
            return False

        # 查找并删除
        for i, v in enumerate(versions):
            if v.version_id == version_id:
                versions.pop(i)
                if versions:
                    current = self._current_version.get(key, len(versions) - 1)
                    if i < current:
                        current -= 1
                    self._current_version[key] = max(0, min(current, len(versions) - 1))
                else:
                    self._current_version.pop(key, None)
                await self._persist_versions(key)
                logger.info("Deleted checkpoint version %s for thread %r ns %r", version_id, thread_id, checkpoint_ns)
                return True

        logger.warning("Version %s not found for thread %r ns %r", version_id, thread_id, checkpoint_ns)
        return False

    async def delete(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
        """删除一个 (thread_id, ns) 的全部版本，并满足 Agent Loop 的 Checkpointer 协议。"""
        key = _storage_key(thread_id, checkpoint_ns)
        self._versions.pop(key, None)
        self._current_version.pop(key, None)
        file_path = self._get_version_file(thread_id, checkpoint_ns)
        if file_path is not None:
            try:
                file_path.unlink(missing_ok=True)
            except OSError as exc:
                raise CheckpointError(f"删除会话 {thread_id!r} ns {checkpoint_ns!r} 的 checkpoint 失败：{exc}") from exc

    async def compare_versions(
        self,
        thread_id: str,
        version_id_1: str,
        version_id_2: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> dict[str, Any]:
        """对比两个版本。

        Args:
            thread_id: 会话 ID。
            version_id_1: 版本 1 ID。
            version_id_2: 版本 2 ID。
            checkpoint_ns: checkpoint 命名空间。

        Returns:
            对比结果（包含差异信息）。
        """
        state1 = await self.load(thread_id, version_id_1, checkpoint_ns=checkpoint_ns)
        state2 = await self.load(thread_id, version_id_2, checkpoint_ns=checkpoint_ns)

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

    def _trim_versions(self, key: str) -> int:
        """把版本数量收敛到 `_max_versions_per_thread` 内，返回淘汰的最旧版本数。

        淘汰只从列表头部（最旧）进行；当前指针所指版本被淘汰时，指针前移到
        它的继任版本（最旧存活版本），未被淘汰时指针仍指向同一版本。
        """
        versions = self._versions.get(key)
        if not versions:
            return 0
        excess = len(versions) - self._max_versions_per_thread
        if excess <= 0:
            return 0
        del versions[:excess]
        current = self._current_version.get(key)
        if current is not None:
            self._current_version[key] = max(current - excess, 0)
        logger.info(
            "Evicted %d oldest checkpoint versions for key %r (limit: %d)",
            excess,
            key,
            self._max_versions_per_thread,
        )
        return excess

    def _restore_current_pointer(self, key: str, previous: CheckpointVersion | None) -> None:
        """按版本身份恢复保存前的当前指针（容量淘汰会改变索引）。"""
        versions = self._versions.get(key, [])
        if previous is None:
            self._current_version.pop(key, None)
            return
        position = next((i for i, item in enumerate(versions) if item is previous), None)
        if position is not None:
            self._current_version[key] = position
        elif versions:
            # 保存前的指针版本已被容量淘汰：指针前移到最旧存活版本。
            self._current_version[key] = 0
        else:
            self._current_version.pop(key, None)

    async def _persist_versions(self, key: str) -> None:
        """持久化版本列表到文件。"""
        if not self._storage_dir:
            return

        # key 格式：thread_id 或 thread_id\x00ns
        ns_sep = key.find("\x00")
        if ns_sep == -1:
            thread_id = key
            checkpoint_ns = DEFAULT_CHECKPOINT_NS
        else:
            thread_id = key[:ns_sep]
            checkpoint_ns = key[ns_sep + 1:]

        file_path = self._get_version_file(thread_id, checkpoint_ns)
        if not file_path:
            return

        versions = self._versions.get(key, [])
        data = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "versions": [v.to_dict() for v in versions],
            "current_version": self._current_version.get(key, 0),
        }

        try:
            _atomic_write_json(file_path, data)
        except Exception as exc:
            raise CheckpointError(f"持久化会话 {thread_id!r} ns {checkpoint_ns!r} 的 checkpoint 版本失败：{exc}") from exc

    async def _load_versions_from_file(self, key: str) -> None:
        """从文件加载版本列表。"""
        if key in self._versions:
            return  # 已加载

        if not self._storage_dir:
            return

        # key 格式：thread_id 或 thread_id\x00ns
        ns_sep = key.find("\x00")
        if ns_sep == -1:
            thread_id = key
            checkpoint_ns = DEFAULT_CHECKPOINT_NS
        else:
            thread_id = key[:ns_sep]
            checkpoint_ns = key[ns_sep + 1:]

        file_path = self._get_version_file(thread_id, checkpoint_ns)
        if not file_path or not file_path.exists():
            return

        try:
            with file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            self._versions[key] = [CheckpointVersion.from_dict(v) for v in data.get("versions", [])]
            self._current_version[key] = data.get("current_version", 0)
            # 历史文件中的版本数可能超过当前上限：加载后同样收敛，指针随淘汰前移。
            self._trim_versions(key)
        except Exception as exc:
            raise CheckpointError(f"加载会话 {thread_id!r} ns {checkpoint_ns!r} 的 checkpoint 版本失败：{exc}") from exc
