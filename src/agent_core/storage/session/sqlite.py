"""基于 SQLite 的会话存储，用于开发与单实例部署。"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from agent_core.access import AccessContext
from agent_core.storage._sqlite_utils import (
    _run_in_thread,
    run_sync_in_thread,
    sqlite_connection,
)
from agent_core.storage.session.base import (
    BaseSessionStore,
    _msg_to_row,
    _row_to_msg,
)


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
        # 建表 DDL/PRAGMA 初始化同样放入工作线程执行，与 async 方法共用同一连接参数规范。
        run_sync_in_thread(self._init_db)
        logger.info("SqliteSessionStore: %s", self._db_path)

    def _init_db(self) -> None:
        """建表（幂等）。"""
        with self._connect() as conn:
            conn.executescript(self.DDL)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """获取 SQLite 连接（同步，自动提交/回滚；统一 timeout 与 WAL 规范）。"""
        with sqlite_connection(self._db_path, row_factory=sqlite3.Row) as conn:
            yield conn

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

    async def load(self, thread_id: str, *, access: AccessContext | None = None) -> list[Message]:
        def _op() -> list[tuple[Any, ...] | dict[str, Any]]:
            with self._connect() as conn:
                self._check_access(conn, thread_id, access, claim=False)
                return conn.execute(
                    "SELECT role, content, name, tool_call_id, tool_calls "
                    "FROM sessions WHERE thread_id = ? ORDER BY id ASC",
                    (thread_id,),
                ).fetchall()

        rows = await _run_in_thread(_op)
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

        def _op() -> None:
            with self._connect() as conn:
                self._check_access(conn, thread_id, access, claim=True)
                conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))
                conn.executemany(
                    "INSERT INTO sessions (thread_id, role, content, name, tool_call_id, tool_calls) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(thread_id, *_msg_to_row(m)) for m in messages],
                )

        await _run_in_thread(_op)
        logger.debug("SQLite save: thread=%r, messages=%d", thread_id, len(messages))

    async def append(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        """追加消息（不需要先 load，效率更高）。"""

        def _op() -> None:
            with self._connect() as conn:
                self._check_access(conn, thread_id, access, claim=True)
                conn.executemany(
                    "INSERT INTO sessions (thread_id, role, content, name, tool_call_id, tool_calls) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(thread_id, *_msg_to_row(m)) for m in messages],
                )

        await _run_in_thread(_op)
        logger.debug("SQLite append: thread=%r, +%d messages", thread_id, len(messages))

    async def delete(self, thread_id: str, *, access: AccessContext | None = None) -> None:
        def _op() -> None:
            with self._connect() as conn:
                self._check_access(conn, thread_id, access, claim=False)
                conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))
                conn.execute("DELETE FROM session_threads WHERE thread_id = ?", (thread_id,))

        await _run_in_thread(_op)
        logger.debug("SQLite delete: thread=%r", thread_id)

    async def list_threads(self, *, access: AccessContext | None = None) -> list[str]:
        if self._require_access and access is None:
            raise PermissionError("该 SessionStore 要求提供 AccessContext")

        def _op() -> list[sqlite3.Row]:
            with self._connect() as conn:
                if access is None:
                    return conn.execute(
                        "SELECT DISTINCT thread_id FROM sessions ORDER BY thread_id"
                    ).fetchall()
                if access.is_admin:
                    return conn.execute(
                        "SELECT thread_id FROM session_threads WHERE tenant_id=? ORDER BY thread_id",
                        (access.tenant_id,),
                    ).fetchall()
                return conn.execute(
                    """SELECT thread_id FROM session_threads
                    WHERE tenant_id=? AND user_id=? ORDER BY thread_id""",
                    (access.tenant_id, access.user_id),
                ).fetchall()

        rows = await _run_in_thread(_op)
        return [row["thread_id"] for row in rows]

    async def count(self, thread_id: str, *, access: AccessContext | None = None) -> int:
        def _op() -> Any:
            with self._connect() as conn:
                self._check_access(conn, thread_id, access, claim=False)
                return conn.execute(
                    "SELECT COUNT(*) AS cnt FROM sessions WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()

        row = await _run_in_thread(_op)
        return int(row["cnt"]) if row else 0

    def db_path(self) -> str:
        """返回数据库文件路径（调试用）。"""
        return self._db_path


# ──────────────────────────────────────────────
# 3. PostgreSQL 后端（生产环境）
# ──────────────────────────────────────────────
