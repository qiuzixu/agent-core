"""轻量级长期上下文存储。

它保存业务引用和用户偏好，而不是把所有历史消息无限塞进 prompt。
应用按环境选择内存、SQLite 或 PostgreSQL；JSON 文件实现仅保留兼容。

存跨会话的业务记忆
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_core.access import AccessContext


# ──────────────────────────────────────────────
# 上下文存储实现
# ──────────────────────────────────────────────
class ContextStore:
    """按 thread 隔离的持久化上下文存储。"""

    def __init__(
        self,
        path: str | Path | None = "./agent_context.json",
        *,
        require_access: bool = False,
    ) -> None:
        self._path = Path(path) if path else None
        self._strict_access = require_access
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def initialize(self) -> None:
        """初始化存储；文件和内存实现不需要额外操作。"""

    async def close(self) -> None:
        """关闭存储；文件和内存实现不需要额外操作。"""

    # 确保上下文数据已加载
    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._path and self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}
        self._loaded = True

    # 刷新上下文数据到文件
    async def _flush(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # 检查上下文数据访问权限
    def _check_scope(
        self,
        data: dict[str, Any],
        access: AccessContext | None,
    ) -> None:
        if self._strict_access and access is None:
            raise PermissionError("该 ContextStore 要求提供 AccessContext")
        owner = data.get("_access")
        if self._strict_access and data and not isinstance(owner, dict):
            raise PermissionError("该 thread 的上下文尚未绑定所有者")
        if (
            access
            and isinstance(owner, dict)
            and not access.can_access(owner.get("user_id"), owner.get("tenant_id"))
        ):
            raise PermissionError("无权访问该 thread 的上下文")

    # 获取上下文数据
    async def get(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._ensure_loaded()
            value = copy.deepcopy(self._data.get(thread_id, {}))
            self._check_scope(value, access)
            return value

    # 更新上下文数据
    async def update(
        self,
        thread_id: str,
        values: dict[str, Any],
        *,
        access: AccessContext | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            await self._ensure_loaded()
            current = self._data.setdefault(thread_id, {})
            self._check_scope(current, access)
            if access and "_access" not in current:
                current["_access"] = access.to_dict()
            current.update(copy.deepcopy(values))
            await self._flush()
            return copy.deepcopy(current)

    # 清除上下文数据
    async def clear(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> None:
        async with self._lock:
            await self._ensure_loaded()
            self._check_scope(self._data.get(thread_id, {}), access)
            self._data.pop(thread_id, None)
            await self._flush()

    # 统一端口名称，同时保留业务层已有的 get/update/clear 调用。
    async def get_context(self, thread_id: str, *, access: AccessContext | None = None) -> dict[str, Any]:
        return await self.get(thread_id, access=access)

    async def update_context(
        self,
        thread_id: str,
        values: dict[str, Any],
        *,
        access: AccessContext | None = None,
    ) -> dict[str, Any]:
        return await self.update(thread_id, values, access=access)

    async def clear_context(self, thread_id: str, *, access: AccessContext | None = None) -> None:
        await self.clear(thread_id, access=access)


# ──────────────────────────────────────────────
# 内存上下文存储实现
# ──────────────────────────────────────────────
class MemoryContextStore(ContextStore):
    """测试专用内存上下文。"""

    def __init__(self, *, require_access: bool = False) -> None:
        super().__init__(path=None, require_access=require_access)


# ──────────────────────────────────────────────
# SQLite 上下文存储实现
# ──────────────────────────────────────────────
class SqliteContextStore(ContextStore):
    """开发环境 SQLite 上下文存储。"""

    def __init__(
        self,
        db_path: str | Path = "./sessions.db",
        *,
        require_access: bool = False,
    ) -> None:
        self._db_path = str(Path(db_path).resolve())
        self._strict_access = require_access
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_context (
                    thread_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL DEFAULT '{}',
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )

    # 连接 SQLite 数据库
    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """打开一个自动提交或回滚的 SQLite 连接。"""
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # 获取上下文数据
    async def get(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM agent_context WHERE thread_id = ?", (thread_id,)).fetchone()
        value = json.loads(row[0]) if row else {}
        self._check_scope(value, access)
        return value

    # 更新上下文数据
    async def update(
        self,
        thread_id: str,
        values: dict[str, Any],
        *,
        access: AccessContext | None = None,
    ) -> dict[str, Any]:
        current = await self.get(thread_id, access=access)
        if access and "_access" not in current:
            current["_access"] = access.to_dict()
        current.update(copy.deepcopy(values))
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO agent_context(thread_id, data) VALUES (?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET data=excluded.data,
                updated_at=CURRENT_TIMESTAMP""",
                (thread_id, json.dumps(current, ensure_ascii=False)),
            )
        return copy.deepcopy(current)

    # 清除上下文数据
    async def clear(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> None:
        await self.get(thread_id, access=access)
        with self._connect() as conn:
            conn.execute("DELETE FROM agent_context WHERE thread_id = ?", (thread_id,))


# ──────────────────────────────────────────────
# PostgreSQL 上下文存储实现
# ──────────────────────────────────────────────
class PostgresContextStore(ContextStore):
    """生产环境 PostgreSQL 上下文存储。"""

    def __init__(self, dsn: str, *, require_access: bool = False) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any = None
        self._strict_access = require_access

    # 初始化 PostgreSQL 数据库连接池
    # 创建上下文表
    async def initialize(self) -> None:
        try:
            import asyncpg  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "PostgresContextStore 需要 asyncpg，请执行：uv sync --extra production"
            ) from exc
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            # 创建上下文表
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_context (
                    thread_id TEXT PRIMARY KEY,
                    data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresContextStore 未初始化")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def get(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> dict[str, Any]:
        self._check()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM agent_context WHERE thread_id=$1",
                thread_id,
            )
        if row is None:
            self._check_scope({}, access)
            return {}
        value = json.loads(row["data"]) if isinstance(row["data"], str) else dict(row["data"])
        self._check_scope(value, access)
        return value

    async def update(
        self,
        thread_id: str,
        values: dict[str, Any],
        *,
        access: AccessContext | None = None,
    ) -> dict[str, Any]:
        self._check()
        current = await self.get(thread_id, access=access)
        if access and "_access" not in current:
            values = {"_access": access.to_dict(), **values}
        async with self._pool.acquire() as conn:
            # 在数据库内合并顶层字段，避免读取后覆盖其他连接刚写入的字段。
            row = await conn.fetchrow(
                """INSERT INTO agent_context(thread_id,data) VALUES ($1,$2::jsonb)
                ON CONFLICT(thread_id) DO UPDATE SET
                data=agent_context.data || EXCLUDED.data, updated_at=NOW()
                RETURNING data""",
                thread_id,
                json.dumps(values, ensure_ascii=False),
            )
        return json.loads(row["data"]) if isinstance(row["data"], str) else dict(row["data"])

    async def clear(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> None:
        await self.get(thread_id, access=access)
        self._check()
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM agent_context WHERE thread_id=$1", thread_id)


# ──────────────────────────────────────────────
# 创建上下文存储
# ──────────────────────────────────────────────
def create_context_store(
    env: str = "development",
    *,
    sqlite_path: str = "./sessions.db",
    postgres_url: str | None = None,
    require_access: bool = False,
) -> ContextStore:
    """按环境创建上下文存储。"""
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryContextStore(require_access=require_access)
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境需要配置 PostgreSQL 连接地址")
        return PostgresContextStore(postgres_url, require_access=require_access)
    return SqliteContextStore(sqlite_path, require_access=require_access)
