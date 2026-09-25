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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from agent_core.access import AccessContext

# ──────────────────────────────────────────────
# 上下文存储实现
# ──────────────────────────────────────────────
class ContextStore:
    """按 thread 隔离的持久化上下文存储。"""

    def __init__(self, path: str | Path | None = "./agent_context.json") -> None:
        self._path = Path(path) if path else None
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
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self._path)

    # 检查上下文数据访问权限
    @staticmethod
    def _check_scope(data: dict[str, Any], access: AccessContext | None) -> None:
        owner = data.get("_access")
        if access and isinstance(owner, dict) and not access.can_access(
            owner.get("user_id"), owner.get("tenant_id")
        ):
            raise PermissionError("无权访问该 thread 的上下文")
    
    # 获取上下文数据
    async def get(self, thread_id: str, *, access: AccessContext | None = None) -> dict[str, Any]:
        async with self._lock:
            await self._ensure_loaded()
            value = copy.deepcopy(self._data.get(thread_id, {}))
            self._check_scope(value, access)
            return value
    
    # 更新上下文数据
    async def update(
        self, thread_id: str, values: dict[str, Any], *, access: AccessContext | None = None
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
    async def clear(self, thread_id: str, *, access: AccessContext | None = None) -> None:
        async with self._lock:
            await self._ensure_loaded()
            self._check_scope(self._data.get(thread_id, {}), access)
            self._data.pop(thread_id, None)
            await self._flush()

    # 统一端口名称，同时保留业务层已有的 get/update/clear 调用。
    async def get_context(self, thread_id: str) -> dict[str, Any]:
        return await self.get(thread_id)

    async def update_context(self, thread_id: str, values: dict[str, Any]) -> dict[str, Any]:
        return await self.update(thread_id, values)

    async def clear_context(self, thread_id: str) -> None:
        await self.clear(thread_id)

# ──────────────────────────────────────────────
# 内存上下文存储实现
# ──────────────────────────────────────────────
class MemoryContextStore(ContextStore):
    """测试专用内存上下文。"""

    def __init__(self) -> None:
        super().__init__(path=None)

# ──────────────────────────────────────────────
# SQLite 上下文存储实现
# ──────────────────────────────────────────────
class SqliteContextStore(ContextStore):
    """开发环境 SQLite 上下文存储。"""

    def __init__(self, db_path: str | Path = "./sessions.db") -> None:
        self._db_path = str(Path(db_path).resolve())
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_context (
                    thread_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL DEFAULT '{}',
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )

    # 连接 SQLite 数据库
    @contextmanager # 上下文管理器，确保数据库连接在使用后关闭
    def _connect(self) -> Generator[sqlite3.Connection, None, None]: # 连接 SQLite 数据库
        conn = sqlite3.connect(self._db_path) # 连接 SQLite 数据库
        try:
            yield conn # 返回数据库连接对象
            conn.commit() # 提交事务
        except Exception:
            conn.rollback() # 回滚事务
            raise # 抛出异常，让调用者处理异常
        finally:
            conn.close() # 关闭数据库连接

    # 获取上下文数据
    async def get(self, thread_id: str, *, access: AccessContext | None = None) -> dict[str, Any]:
        with self._connect() as conn: # 连接 SQLite 数据库
            row = conn.execute(
                "SELECT data FROM agent_context WHERE thread_id = ?", (thread_id,)
            ).fetchone() # 查询上下文数据
        value = json.loads(row[0]) if row else {} # 解析 JSON 字符串为字典
        self._check_scope(value, access) # 检查上下文数据访问权限
        return value     # 返回上下文数据

    # 更新上下文数据
    async def update(self, thread_id: str, values: dict[str, Any], *, access: AccessContext | None = None) -> dict[str, Any]:
        current = await self.get(thread_id, access=access) # 获取当前上下文数据
        if access and "_access" not in current: # 检查上下文数据是否包含访问权限
            current["_access"] = access.to_dict() # 添加访问权限
        current.update(copy.deepcopy(values)) # 更新上下文数据
        with self._connect() as conn: # 连接 SQLite 数据库
            conn.execute( # 插入或更新上下文数据
                """INSERT INTO agent_context(thread_id, data) VALUES (?, ?) 
                ON CONFLICT(thread_id) DO UPDATE SET data=excluded.data,
                updated_at=CURRENT_TIMESTAMP""",
                (thread_id, json.dumps(current, ensure_ascii=False)),
            )
        return copy.deepcopy(current)
    
    # 清除上下文数据
    async def clear(self, thread_id: str, *, access: AccessContext | None = None) -> None:
        await self.get(thread_id, access=access) # 获取上下文数据
        with self._connect() as conn: # 连接 SQLite 数据库
            conn.execute("DELETE FROM agent_context WHERE thread_id = ?", (thread_id,)) # 删除上下文数据

# ──────────────────────────────────────────────
# PostgreSQL 上下文存储实现
# ──────────────────────────────────────────────
class PostgresContextStore(ContextStore):
    """生产环境 PostgreSQL 上下文存储。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://") # 替换 asyncpg 协议为 PostgreSQL 协议
        self._pool: Any = None

    # 初始化 PostgreSQL 数据库连接池
    # 创建上下文表
    async def initialize(self) -> None:
        try:
            import asyncpg  # type: ignore[import-untyped] 
        except ImportError as exc:
            raise ImportError("PostgresContextStore 需要 asyncpg，请执行：uv sync --extra production") from exc
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10) # 创建 PostgreSQL 数据库连接池
        async with self._pool.acquire() as conn: # 获取数据库连接
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

    async def get(self, thread_id: str, *, access: AccessContext | None = None) -> dict[str, Any]:
        self._check()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT data FROM agent_context WHERE thread_id=$1", thread_id)
        if row is None:
            return {}
        value = json.loads(row["data"]) if isinstance(row["data"], str) else dict(row["data"])
        self._check_scope(value, access)
        return value

    async def update(self, thread_id: str, values: dict[str, Any], *, access: AccessContext | None = None) -> dict[str, Any]:
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

    async def clear(self, thread_id: str, *, access: AccessContext | None = None) -> None:
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
) -> ContextStore:
    """按环境创建上下文存储。"""
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryContextStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境需要配置 PostgreSQL 连接地址")
        return PostgresContextStore(postgres_url)
    return SqliteContextStore(sqlite_path)
