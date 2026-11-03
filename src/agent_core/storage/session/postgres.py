"""基于 PostgreSQL 的会话存储，用于生产与多实例部署。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from agent_core.access import AccessContext
from agent_core.storage.session.base import (
    BaseSessionStore,
    _msg_to_row,
    _row_to_msg,
)


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
            import asyncpg
        except ImportError as exc:
            raise ImportError("PostgresSessionStore 需要 asyncpg，请执行：uv add asyncpg") from exc

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
            raise RuntimeError("PostgresSessionStore 未初始化，请先调用 await store.initialize()")

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
        if row is not None and access is not None and not access.can_access(row["user_id"], row["tenant_id"]):
            raise PermissionError("无权访问该 thread 的会话历史")

    async def load(self, thread_id: str, *, access: AccessContext | None = None) -> list[Message]:
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
                await conn.execute("DELETE FROM sessions WHERE thread_id = $1", thread_id)
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

    async def delete(self, thread_id: str, *, access: AccessContext | None = None) -> None:
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
                rows = await conn.fetch("SELECT DISTINCT thread_id FROM sessions ORDER BY thread_id")
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

    async def count(self, thread_id: str, *, access: AccessContext | None = None) -> int:
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
