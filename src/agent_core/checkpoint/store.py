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
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agent_core.errors import CheckpointError

logger = logging.getLogger(__name__)

# 文件名严格清洗：白名单字符原样保留（可读性），其余按 UTF-8 字节转义为 %XX。
# Windows 非法字符 <>:"/\|?*、``%``（转义前缀，必须一并转义保证可逆）和控制符都要处理。
_FILENAME_ILLEGAL_CHARS = frozenset('<>:"/\\|?*%')
_FILENAME_ESCAPE_CHARS = _FILENAME_ILLEGAL_CHARS | {chr(code) for code in range(0x20)}
# Windows 保留设备名（不区分大小写，且无论后缀如何都保留）：只检查文件名第一个点之前的主干。
_WINDOWS_RESERVED = re.compile(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", re.IGNORECASE)
_HEX_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


def _escape_char(char: str) -> str:
    """把单个字符转义为 UTF-8 字节的 %XX 序列。"""
    return "".join(f"%{byte:02X}" for byte in char.encode("utf-8", errors="surrogatepass"))


def _sanitize_thread_id(thread_id: str) -> str:
    """把 thread_id 清洗为跨平台安全且可逆的文件名主干。

    规则：
    - 白名单（可打印且不属于非法字符集）原样保留，保证可读性；
    - 其余字符（Windows 非法字符 ``<>:"/\\|?*``、``%``、控制符、孤立代理对等）
      按 UTF-8 字节转义为 ``%XX``。``%`` 本身也被转义，因此清洗是单射：
      ``a/b`` 与 ``a_b`` 会得到不同文件名，不再互相覆盖；
    - 以点/空格结尾或命中 Windows 保留设备名（CON、PRN、COM1 等）时，
      对首个点之前主干的末字符转义规避系统特殊处理，映射仍然可逆。
    """
    sanitized = "".join(
        char if char.isprintable() and char not in _FILENAME_ESCAPE_CHARS else _escape_char(char)
        for char in thread_id
    )
    if sanitized.endswith((".", " ")):
        sanitized = sanitized[:-1] + _escape_char(sanitized[-1])
    base_name = sanitized.split(".", 1)[0]
    if _WINDOWS_RESERVED.fullmatch(base_name):
        sanitized = sanitized[:-1] + _escape_char(sanitized[-1])
    return sanitized


def _restore_thread_id(file_stem: str) -> str:
    """`_sanitize_thread_id` 的逆映射；无法还原的历史文件名原样返回。"""
    if "%" not in file_stem:
        return file_stem
    raw = bytearray()
    index = 0
    while index < len(file_stem):
        match = _HEX_ESCAPE.match(file_stem, index)
        if match is None:
            raw.extend(file_stem[index].encode("utf-8", errors="surrogatepass"))
            index += 1
        else:
            raw.append(int(match.group(1), 16))
            index = match.end()
    return raw.decode("utf-8", errors="surrogatepass")


def _atomic_write_json(file_path: Path, value: Any) -> None:
    """在目标目录写临时文件并原子替换，避免崩溃留下截断 JSON。"""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=file_path.parent,
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(value, temporary, ensure_ascii=False, indent=2)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, file_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


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
