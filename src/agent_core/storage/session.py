"""会话存储：多轮对话历史持久化。

存储后端（通过 create_session_store() 工厂自动选择）：
    - MemorySessionStore   : 进程内存，重启丢失（单元测试用）
    - SqliteSessionStore   : SQLite 本地文件，开发环境首选
    - PostgresSessionStore : PostgreSQL，生产环境首选

选择逻辑（由 create_session_store() 控制）：
    AGENT_ENV=development（默认）→ SqliteSessionStore  (./sessions.db)
    AGENT_ENV=production         → PostgresSessionStore (需配置 AGENT_SESSION_POSTGRES_URL)

对应关系（和 LangGraph 类比）：
    MemorySessionStore   ≈ MemorySaver      (测试)
    SqliteSessionStore   ≈ SqliteSaver      (开发)
    PostgresSessionStore ≈ PostgresSaver    (生产)

表结构（SQLite / PostgreSQL 通用）：
    sessions
    ├── id           INTEGER  PRIMARY KEY AUTOINCREMENT
    ├── thread_id    TEXT     NOT NULL
    ├── role         TEXT     NOT NULL   (user/assistant/tool/system)
    ├── content      TEXT     NOT NULL
    ├── name         TEXT
    ├── tool_call_id TEXT
    ├── tool_calls   TEXT               (JSON 序列化)
    └── created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_core.access import AccessContext
from agent_core.model import ContextUsage
from agent_core.protocol.messages import Message, assistant_message, user_message
from agent_core.protocol.runtime import RunContext

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 消息序列化 / 反序列化
# ──────────────────────────────────────────────

def _msg_to_row(msg: Message) -> tuple[str, str, str | None, str | None, str]:
    """Message → (role, content, name, tool_call_id, tool_calls_json)。"""
    return (
        msg.role,
        msg.content,
        msg.name,
        msg.tool_call_id,
        json.dumps(msg.tool_calls, ensure_ascii=False) if msg.tool_calls else "[]",
    )


# ──────────────────────────────────────────────
# 数据库行 → Message
# ──────────────────────────────────────────────
def _row_to_msg(row: tuple | dict) -> Message:
    """数据库行 → Message。

    兼容 sqlite3.Row（可按列名访问）和普通 tuple。
    """
    if isinstance(row, dict):
        role, content, name, tool_call_id, tool_calls_json = (
            row["role"], row["content"], row.get("name"),
            row.get("tool_call_id"), row.get("tool_calls", "[]"),
        )
    else:
        # tuple: (role, content, name, tool_call_id, tool_calls)
        role, content, name, tool_call_id, tool_calls_json = row

    tool_calls: list[dict[str, Any]] = []
    if tool_calls_json:
        try:
            tool_calls = json.loads(tool_calls_json)
        except json.JSONDecodeError:
            pass

    return Message(
        role=role,
        content=content,
        name=name,
        tool_call_id=tool_call_id,
        tool_calls=tool_calls,
    )


# ──────────────────────────────────────────────
# 抽象接口
# ──────────────────────────────────────────────

class BaseSessionStore(ABC):
    """会话存储抽象基类。"""

    @abstractmethod
    async def load(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> list[Message]:
        """加载指定 thread 的消息历史。"""

    @abstractmethod
    async def save(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        """覆盖保存指定 thread 的消息历史。"""

    @abstractmethod
    async def append(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        """追加消息到指定 thread（比 save 更高效，不需要先 load）。"""

    @abstractmethod
    async def delete(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> None:
        """删除指定 thread 的所有消息。"""

    @abstractmethod
    async def list_threads(self, *, access: AccessContext | None = None) -> list[str]:
        """列出所有 thread_id。"""

    @abstractmethod
    async def count(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> int:
        """返回指定 thread 的消息数量。"""

    # 统一端口名称，同时保留早期实现中的短方法名。
    async def load_messages(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> list[Message]:
        return await self.load(thread_id, access=access)

    async def append_messages(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        await self.append(thread_id, messages, access=access)

    async def replace_messages(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        await self.save(thread_id, messages, access=access)

    async def delete_thread(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> None:
        await self.delete(thread_id, access=access)


# ──────────────────────────────────────────────
# 1. 内存后端（测试用）
# ──────────────────────────────────────────────

class MemorySessionStore(BaseSessionStore):
    """进程内存，重启丢失。仅用于单元测试。"""

    def __init__(self, *, require_access: bool = False) -> None:
        # thread_id → list[Message]
        self._data: dict[str, list[Message]] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._require_access = require_access

    def _check(self, thread_id: str, access: AccessContext | None, *, claim: bool) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 SessionStore 要求提供 AccessContext")
        owner = self._owners.get(thread_id)
        if owner is None and access is not None and claim:
            self._owners[thread_id] = (access.user_id, access.tenant_id)
            return
        if owner is None and self._require_access and thread_id in self._data:
            raise PermissionError("该 thread 的历史数据尚未绑定所有者")
        if owner is not None and access is not None and not access.can_access(*owner):
            raise PermissionError("无权访问该 thread 的会话历史")

    async def load(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> list[Message]:
        self._check(thread_id, access, claim=False)
        return list(self._data.get(thread_id, []))

    async def save(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        self._check(thread_id, access, claim=True)
        self._data[thread_id] = list(messages)

    async def append(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        self._check(thread_id, access, claim=True)
        self._data.setdefault(thread_id, []).extend(messages)

    async def delete(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> None:
        self._check(thread_id, access, claim=False)
        self._data.pop(thread_id, None)
        self._owners.pop(thread_id, None)

    async def list_threads(self, *, access: AccessContext | None = None) -> list[str]:
        if self._require_access and access is None:
            raise PermissionError("该 SessionStore 要求提供 AccessContext")
        if access is None:
            return list(self._data.keys())
        return [
            thread_id
            for thread_id in self._data
            if (owner := self._owners.get(thread_id)) is not None
            and access.can_access(*owner)
        ]

    async def count(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> int:
        self._check(thread_id, access, claim=False)
        return len(self._data.get(thread_id, []))


# ──────────────────────────────────────────────
# 2. SQLite 后端（开发环境）
# ──────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER  PRIMARY KEY AUTOINCREMENT,
    thread_id    TEXT     NOT NULL,
    role         TEXT     NOT NULL,
    content      TEXT     NOT NULL,
    name         TEXT,
    tool_call_id TEXT,
    tool_calls   TEXT     NOT NULL DEFAULT '[]',
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sessions_thread_id ON sessions (thread_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions (thread_id, created_at);
CREATE TABLE IF NOT EXISTS session_threads (
    thread_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# ──────────────────────────────────────────────
# 3. SQLite 后端（开发环境）
# ──────────────────────────────────────────────
class SqliteSessionStore(BaseSessionStore):
    """SQLite 本地文件存储。

    - 开发环境默认使用
    - 零额外依赖（Python 内置 sqlite3）
    - 重启后数据不丢失
    - 不适合多进程并发写（多实例部署请用 PostgreSQL）

    文件位置：由 db_path 参数指定，默认 ./sessions.db
    """

    DDL = _SCHEMA_SQL

    def __init__(
        self,
        db_path: str | Path = "./sessions.db",
        *,
        require_access: bool = False,
    ) -> None:
        self._db_path = str(Path(db_path).resolve())
        self._require_access = require_access
        self._init_db()
        logger.info("SqliteSessionStore: %s", self._db_path)

    def _init_db(self) -> None:
        """建表（幂等）。"""
        with self._connect() as conn:
            conn.executescript(self.DDL)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """获取 SQLite 连接（同步，自动提交/回滚）。"""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # WAL 模式，提升并发读性能
        conn.execute("PRAGMA synchronous=NORMAL") # 平衡安全和性能
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _check_access(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        access: AccessContext | None,
        *,
        claim: bool,
    ) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 SessionStore 要求提供 AccessContext")
        row = conn.execute(
            "SELECT user_id,tenant_id FROM session_threads WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if row is None and access is not None and claim:
            conn.execute(
                "INSERT INTO session_threads(thread_id,user_id,tenant_id) VALUES (?,?,?)",
                (thread_id, access.user_id, access.tenant_id),
            )
            return
        if row is None and self._require_access:
            has_messages = conn.execute(
                "SELECT 1 FROM sessions WHERE thread_id=? LIMIT 1",
                (thread_id,),
            ).fetchone()
            if has_messages is not None:
                raise PermissionError("该 thread 的历史数据尚未绑定所有者")
        if row is not None and access is not None and not access.can_access(row[0], row[1]):
            raise PermissionError("无权访问该 thread 的会话历史")

    async def load(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> list[Message]:
        with self._connect() as conn:
            self._check_access(conn, thread_id, access, claim=False)
            rows = conn.execute(
                "SELECT role, content, name, tool_call_id, tool_calls "
                "FROM sessions WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
        result = [_row_to_msg(dict(row)) for row in rows]
        logger.debug("SQLite load: thread=%r, messages=%d", thread_id, len(result))
        return result

    async def save(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        """覆盖保存（先删再插）。"""
        with self._connect() as conn:
            self._check_access(conn, thread_id, access, claim=True)
            conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))
            conn.executemany(
                "INSERT INTO sessions (thread_id, role, content, name, tool_call_id, tool_calls) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(thread_id, *_msg_to_row(m)) for m in messages],
            )
        logger.debug("SQLite save: thread=%r, messages=%d", thread_id, len(messages))

    async def append(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        """追加消息（不需要先 load，效率更高）。"""
        with self._connect() as conn:
            self._check_access(conn, thread_id, access, claim=True)
            conn.executemany(
                "INSERT INTO sessions (thread_id, role, content, name, tool_call_id, tool_calls) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(thread_id, *_msg_to_row(m)) for m in messages],
            )
        logger.debug("SQLite append: thread=%r, +%d messages", thread_id, len(messages))

    async def delete(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> None:
        with self._connect() as conn:
            self._check_access(conn, thread_id, access, claim=False)
            conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM session_threads WHERE thread_id = ?", (thread_id,))
        logger.debug("SQLite delete: thread=%r", thread_id)

    async def list_threads(self, *, access: AccessContext | None = None) -> list[str]:
        if self._require_access and access is None:
            raise PermissionError("该 SessionStore 要求提供 AccessContext")
        with self._connect() as conn:
            if access is None:
                rows = conn.execute(
                    "SELECT DISTINCT thread_id FROM sessions ORDER BY thread_id"
                ).fetchall()
            elif access.is_admin:
                rows = conn.execute(
                    "SELECT thread_id FROM session_threads WHERE tenant_id=? ORDER BY thread_id",
                    (access.tenant_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT thread_id FROM session_threads
                    WHERE tenant_id=? AND user_id=? ORDER BY thread_id""",
                    (access.tenant_id, access.user_id),
                ).fetchall()
        return [row["thread_id"] for row in rows]

    async def count(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> int:
        with self._connect() as conn:
            self._check_access(conn, thread_id, access, claim=False)
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return row["cnt"] if row else 0

    def db_path(self) -> str:
        """返回数据库文件路径（调试用）。"""
        return self._db_path


# ──────────────────────────────────────────────
# 3. PostgreSQL 后端（生产环境）
# ──────────────────────────────────────────────

class PostgresSessionStore(BaseSessionStore):
    """PostgreSQL 异步存储。

    - 生产环境使用
    - 支持多实例并发写
    - 需要安装 asyncpg：uv add asyncpg

    连接 URL 格式：
        postgresql://user:password@host:5432/dbname
        postgresql+asyncpg://user:password@host:5432/dbname

    使用方式：
        store = PostgresSessionStore("postgresql://user:pass@localhost/mydb")
        await store.initialize()   # 建表（幂等，启动时调用一次）
    """

    DDL = """
    CREATE TABLE IF NOT EXISTS sessions (
        id           SERIAL       PRIMARY KEY,
        thread_id    TEXT         NOT NULL,
        role         TEXT         NOT NULL,
        content      TEXT         NOT NULL,
        name         TEXT,
        tool_call_id TEXT,
        tool_calls   TEXT         NOT NULL DEFAULT '[]',
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_thread_id  ON sessions (thread_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions (thread_id, created_at);
    CREATE TABLE IF NOT EXISTS session_threads (
        thread_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """

    def __init__(self, dsn: str, *, require_access: bool = False) -> None:
        # 统一转成 asyncpg 格式（去掉 +asyncpg 前缀）
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any = None  # asyncpg.Pool
        self._require_access = require_access

    async def initialize(self) -> None:
        """创建连接池并建表（启动时调用一次）。"""
        try:
            import asyncpg  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "PostgresSessionStore 需要 asyncpg，请执行：uv add asyncpg"
            ) from exc

        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(self.DDL)
        logger.info("PostgresSessionStore initialized: %s", self._dsn.split("@")[-1])

    async def close(self) -> None:
        """关闭连接池（服务停止时调用）。"""
        if self._pool:
            await self._pool.close()

    def _check_pool(self) -> None:
        if self._pool is None:
            raise RuntimeError(
                "PostgresSessionStore 未初始化，请先调用 await store.initialize()"
            )

    async def _check_access(
        self,
        connection: Any,
        thread_id: str,
        access: AccessContext | None,
        *,
        claim: bool,
    ) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 SessionStore 要求提供 AccessContext")
        row = await connection.fetchrow(
            "SELECT user_id,tenant_id FROM session_threads WHERE thread_id=$1",
            thread_id,
        )
        if row is None and access is not None and claim:
            await connection.execute(
                """INSERT INTO session_threads(thread_id,user_id,tenant_id)
                VALUES ($1,$2,$3) ON CONFLICT(thread_id) DO NOTHING""",
                thread_id,
                access.user_id,
                access.tenant_id,
            )
            row = await connection.fetchrow(
                "SELECT user_id,tenant_id FROM session_threads WHERE thread_id=$1",
                thread_id,
            )
        if row is None and self._require_access:
            has_messages = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM sessions WHERE thread_id=$1)",
                thread_id,
            )
            if has_messages:
                raise PermissionError("该 thread 的历史数据尚未绑定所有者")
        if row is not None and access is not None and not access.can_access(
            row["user_id"], row["tenant_id"]
        ):
            raise PermissionError("无权访问该 thread 的会话历史")

    async def load(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> list[Message]:
        self._check_pool()
        async with self._pool.acquire() as conn:
            await self._check_access(conn, thread_id, access, claim=False)
            rows = await conn.fetch(
                "SELECT role, content, name, tool_call_id, tool_calls "
                "FROM sessions WHERE thread_id = $1 ORDER BY id ASC",
                thread_id,
            )
        result = [_row_to_msg(dict(row)) for row in rows]
        logger.debug("Postgres load: thread=%r, messages=%d", thread_id, len(result))
        return result

    async def save(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        self._check_pool()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._check_access(conn, thread_id, access, claim=True)
                await conn.execute(
                    "DELETE FROM sessions WHERE thread_id = $1", thread_id
                )
                if messages:
                    await conn.executemany(
                        "INSERT INTO sessions "
                        "(thread_id, role, content, name, tool_call_id, tool_calls) "
                        "VALUES ($1, $2, $3, $4, $5, $6)",
                        [(thread_id, *_msg_to_row(m)) for m in messages],
                    )
        logger.debug("Postgres save: thread=%r, messages=%d", thread_id, len(messages))

    async def append(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        self._check_pool()
        async with self._pool.acquire() as conn:
            await self._check_access(conn, thread_id, access, claim=True)
            await conn.executemany(
                "INSERT INTO sessions "
                "(thread_id, role, content, name, tool_call_id, tool_calls) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                [(thread_id, *_msg_to_row(m)) for m in messages],
            )
        logger.debug("Postgres append: thread=%r, +%d messages", thread_id, len(messages))

    async def delete(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> None:
        self._check_pool()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._check_access(conn, thread_id, access, claim=False)
                await conn.execute("DELETE FROM sessions WHERE thread_id = $1", thread_id)
                await conn.execute("DELETE FROM session_threads WHERE thread_id = $1", thread_id)

    async def list_threads(self, *, access: AccessContext | None = None) -> list[str]:
        if self._require_access and access is None:
            raise PermissionError("该 SessionStore 要求提供 AccessContext")
        self._check_pool()
        async with self._pool.acquire() as conn:
            if access is None:
                rows = await conn.fetch(
                    "SELECT DISTINCT thread_id FROM sessions ORDER BY thread_id"
                )
            elif access.is_admin:
                rows = await conn.fetch(
                    "SELECT thread_id FROM session_threads WHERE tenant_id=$1 ORDER BY thread_id",
                    access.tenant_id,
                )
            else:
                rows = await conn.fetch(
                    """SELECT thread_id FROM session_threads
                    WHERE tenant_id=$1 AND user_id=$2 ORDER BY thread_id""",
                    access.tenant_id,
                    access.user_id,
                )
        return [row["thread_id"] for row in rows]

    async def count(
        self, thread_id: str, *, access: AccessContext | None = None
    ) -> int:
        self._check_pool()
        async with self._pool.acquire() as conn:
            await self._check_access(conn, thread_id, access, claim=False)
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt FROM sessions WHERE thread_id = $1",
                thread_id,
            )
        return row["cnt"] if row else 0


# ──────────────────────────────────────────────
# 4. 工厂函数（环境感知自动切换）
# ──────────────────────────────────────────────

def create_session_store(
    env: str = "development",
    *,
    sqlite_path: str = "./sessions.db",
    postgres_url: str | None = None,
    require_access: bool = False,
) -> BaseSessionStore:
    """根据运行环境创建合适的会话存储后端。

    对应关系（和 LangGraph 类比）：
        development → SqliteSessionStore   ≈ SqliteSaver
        production  → PostgresSessionStore ≈ PostgresSaver

    Args:
        env: 运行环境，"development"（默认）或 "production"。
        sqlite_path: SQLite 数据库文件路径。
        postgres_url: PostgreSQL 连接 URL（生产环境必填）。
        require_access: 是否强制所有调用携带 ``AccessContext``。

    Returns:
        对应的存储后端实例。

    Raises:
        ValueError: 生产环境未配置 postgres_url。
    """
    normalized = env.lower()
    if normalized in ("test", "testing"):
        logger.info("Session store: memory (test)")
        return MemorySessionStore(require_access=require_access)

    is_prod = normalized in ("production", "prod")

    if is_prod:
        if not postgres_url:
            raise ValueError(
                "生产环境需要配置 AGENT_SESSION_POSTGRES_URL\n"
                "例如：postgresql://user:pass@localhost:5432/agent_db"
            )
        logger.info("Session store: PostgreSQL (production)")
        return PostgresSessionStore(postgres_url, require_access=require_access)

    logger.info("Session store: SQLite (development) → %s", sqlite_path)
    return SqliteSessionStore(sqlite_path, require_access=require_access)


# ──────────────────────────────────────────────
# 5. 会话管理器（组合 Agent + Store）
# ──────────────────────────────────────────────

class SessionManager:
    """带多轮记忆的对话管理器。

    使用示例：
        # 开发环境（自动用 SQLite）
        mgr = SessionManager.from_config(env="development")

        # 生产环境（自动用 PostgreSQL）
        mgr = SessionManager.from_config(
            env="production",
            postgres_url="postgresql://user:pass@host/db",
        )

        # 每轮对话
        answer = await mgr.chat(agent, thread_id="user-123", user_input="你好")
        answer = await mgr.chat(agent, thread_id="user-123", user_input="我叫什么")
    """

    def __init__(
        self,
        store: BaseSessionStore,
        *,
        max_history_messages: int = 50,
    ) -> None:
        self._store = store
        self._max_history = max_history_messages
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_config(
        cls,
        env: str = "development",
        *,
        sqlite_path: str = "./sessions.db",
        postgres_url: str | None = None,
        max_history_messages: int = 50,
        require_access: bool = False,
    ) -> "SessionManager":
        """从配置创建 SessionManager（自动选择存储后端）。"""
        store = create_session_store(
            env,
            sqlite_path=sqlite_path,
            postgres_url=postgres_url,
            require_access=require_access,
        )
        return cls(store, max_history_messages=max_history_messages)

    async def setup(self) -> None:
        """初始化存储（PostgreSQL 需要调用建连接池和建表）。"""
        if isinstance(self._store, PostgresSessionStore):
            await self._store.initialize()

    async def teardown(self) -> None:
        """关闭存储（服务停止时调用）。"""
        if isinstance(self._store, PostgresSessionStore):
            await self._store.close()

    async def chat(
        self,
        agent: Any,
        thread_id: str,
        user_input: str,
        *,
        append_user: bool = True,
        run_context: RunContext | None = None,
        checkpoint_state: dict[str, Any] | None = None,
        access: AccessContext | None = None,
    ) -> str:
        """串行化同一 thread 的对话，避免并发请求覆盖历史。"""
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            return await self._chat_unlocked(
                agent,
                thread_id,
                user_input,
                append_user=append_user,
                run_context=run_context,
                checkpoint_state=checkpoint_state,
                access=access,
            )

    async def _chat_unlocked(
        self,
        agent: Any,
        thread_id: str,
        user_input: str,
        *,
        append_user: bool = True,
        run_context: RunContext | None = None,
        checkpoint_state: dict[str, Any] | None = None,
        access: AccessContext | None = None,
    ) -> str:
        """带记忆的多轮对话。

        Args:
            agent: ReActAgent 实例。
            thread_id: 会话 ID。
            user_input: 用户输入。

        Returns:
            Agent 回答文本。
        """
        if access is None and run_context is not None:
            if run_context.user_id is not None and run_context.tenant_id is not None:
                access = AccessContext(
                    user_id=run_context.user_id,
                    tenant_id=run_context.tenant_id,
                )

        # 1. 加载历史，构建 memory_context
        history = await self._store.load(thread_id, access=access)
        memory_context = self._build_context(history) if history else None

        # 2. 裁剪过长历史（仅在内存中裁剪，下一步 append 写入的是新消息）
        if len(history) > self._max_history:
            history = history[-self._max_history:]
            await self._store.save(thread_id, history, access=access)

        # 3. 调用 Agent
        answer = await agent.run(
            user_input,
            memory_context=memory_context,
            thread_id=thread_id,
            run_context=run_context,
            checkpoint_state=checkpoint_state,
        )

        # 4. 追加本轮对话（append 比 save 更高效，只写新增的行）。
        # 暂停的 run 在等待浏览器审批前已经保存过用户消息，因此恢复时不重复追加。
        messages_to_append = [assistant_message(answer)]
        if append_user:
            messages_to_append.insert(0, user_message(user_input))
        await self._store.append(thread_id, messages_to_append, access=access)

        return answer

    def _build_context(self, history: list[Message]) -> str:
        """把历史消息转成字符串摘要注入 system prompt。"""
        lines: list[str] = []
        for msg in history:
            if msg.role == "user":
                lines.append(f"用户: {msg.content}")
            elif msg.role == "assistant":
                # 限制每条摘要长度，避免 context 过长
                lines.append(f"助手: {msg.content[:300]}")
        return "\n".join(lines)

    async def get_history(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> list[Message]:
        """获取会话历史（用于查询接口）。"""
        return await self._store.load(thread_id, access=access)

    # ──────────────────────────────────────────────
    # 上下文使用
    # ──────────────────────────────────────────────
    async def context_usage(
        self,
        agent: Any,
        thread_id: str | None,
        user_input: str,
        *,
        access: AccessContext | None = None,
    ) -> ContextUsage:
        """按真实会话记忆构造下一次模型输入并统计 token。"""
        history = await self._store.load(thread_id, access=access) if thread_id else []
        memory_context = self._build_context(history) if history else None
        return await agent.context_usage(user_input, memory_context=memory_context)

    async def clear(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> None:
        """清空会话历史。"""
        await self._store.delete(thread_id, access=access)
        lock = self._locks.get(thread_id)
        if lock is not None and not lock.locked():
            self._locks.pop(thread_id, None)

    async def list_threads(
        self,
        *,
        access: AccessContext | None = None,
    ) -> list[str]:
        """列出所有会话 ID。"""
        return await self._store.list_threads(access=access)

    @property
    def store(self) -> BaseSessionStore:
        """暴露底层存储（测试用）。"""
        return self._store
