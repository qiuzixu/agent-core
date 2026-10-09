"""Run 分布式租约存储。

租约只负责保证同一个 Run 同一时间由一个 Worker 执行，不替代 RunContext、
checkpoint 或任务队列。Worker 必须定期续租，租约过期后其他 Worker 才能接管。
"""

from __future__ import annotations

import asyncio
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_core.storage._sqlite_utils import (
    _run_in_thread,
    run_sync_in_thread,
    sqlite_connection,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RunLease:
    """一个 Run 当前的 Worker 所有权。"""

    thread_id: str
    run_id: str
    worker_id: str
    lease_expires_at: str
    heartbeat_at: str

    @property
    def expired(self) -> bool:
        return _parse(self.lease_expires_at) <= _now()


class RunLeaseStore(ABC):
    """Run 租约存储端口。"""

    async def initialize(self) -> None:
        """初始化后端。内存和 SQLite 实现无需额外操作。"""
        return None

    async def close(self) -> None:
        """关闭后端。内存和 SQLite 实现无需额外操作。"""
        return None

    @abstractmethod
    async def claim(
        self,
        thread_id: str,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        """认领 Run；已有未过期租约时返回 False。"""

    @abstractmethod
    async def renew(
        self,
        thread_id: str,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        """只有当前所有者可以续租。"""

    @abstractmethod
    async def release(self, thread_id: str, run_id: str, worker_id: str) -> bool:
        """只有当前所有者可以释放租约。"""

    @abstractmethod
    async def list_expired(self) -> list[RunLease]:
        """列出已经过期、可以恢复的租约。"""


class MemoryRunLeaseStore(RunLeaseStore):
    """测试和单进程开发使用的内存租约。"""

    def __init__(self) -> None:
        self._leases: dict[tuple[str, str], RunLease] = {}
        self._lock = asyncio.Lock()

    async def claim(
        self,
        thread_id: str,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        _validate_ttl(ttl_seconds)
        async with self._lock:
            key = (thread_id, run_id)
            current = self._leases.get(key)
            if current is not None and not current.expired and current.worker_id != worker_id:
                return False
            now = _now()
            self._leases[key] = RunLease(
                thread_id=thread_id,
                run_id=run_id,
                worker_id=worker_id,
                heartbeat_at=_iso(now),
                lease_expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
            )
            return True

    async def renew(
        self,
        thread_id: str,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        _validate_ttl(ttl_seconds)
        async with self._lock:
            key = (thread_id, run_id)
            current = self._leases.get(key)
            if current is None or current.worker_id != worker_id or current.expired:
                return False
            now = _now()
            self._leases[key] = RunLease(
                thread_id=thread_id,
                run_id=run_id,
                worker_id=worker_id,
                heartbeat_at=_iso(now),
                lease_expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
            )
            return True

    async def release(self, thread_id: str, run_id: str, worker_id: str) -> bool:
        async with self._lock:
            key = (thread_id, run_id)
            current = self._leases.get(key)
            if current is None or current.worker_id != worker_id:
                return False
            del self._leases[key]
            return True

    async def list_expired(self) -> list[RunLease]:
        async with self._lock:
            return [lease for lease in self._leases.values() if lease.expired]


class SqliteRunLeaseStore(RunLeaseStore):
    """开发环境 SQLite 租约实现。"""

    DDL = """
    CREATE TABLE IF NOT EXISTS agent_run_leases (
        thread_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        worker_id TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        PRIMARY KEY (thread_id, run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_agent_run_leases_expiry
    ON agent_run_leases(lease_expires_at);
    """

    def __init__(self, db_path: str | Path = "./sessions.db") -> None:
        self._db_path = str(Path(db_path).resolve())
        # 建表 DDL/PRAGMA 初始化同样放入工作线程执行，与 async 方法共用同一连接参数规范。
        run_sync_in_thread(self._init_db)

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(self.DDL)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """获取 SQLite 连接（同步，自动提交/回滚；统一 timeout 与 WAL 规范）。"""
        with sqlite_connection(self._db_path) as connection:
            yield connection

    async def claim(
        self,
        thread_id: str,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        _validate_ttl(ttl_seconds)
        now = _now()
        expires_at = now + timedelta(seconds=ttl_seconds)

        def _op() -> bool:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT worker_id, lease_expires_at FROM agent_run_leases WHERE thread_id=? AND run_id=?",
                    (thread_id, run_id),
                ).fetchone()
                if row and row[0] != worker_id and _parse(row[1]) > now:
                    return False
                connection.execute(
                    """INSERT INTO agent_run_leases
                    (thread_id,run_id,worker_id,lease_expires_at,heartbeat_at)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(thread_id,run_id) DO UPDATE SET
                    worker_id=excluded.worker_id,
                    lease_expires_at=excluded.lease_expires_at,
                    heartbeat_at=excluded.heartbeat_at""",
                    (thread_id, run_id, worker_id, _iso(expires_at), _iso(now)),
                )
            return True

        return await _run_in_thread(_op)

    async def renew(
        self,
        thread_id: str,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        _validate_ttl(ttl_seconds)
        now = _now()

        def _op() -> int:
            with self._connect() as connection:
                cursor = connection.execute(
                    """UPDATE agent_run_leases
                    SET lease_expires_at=?, heartbeat_at=?
                    WHERE thread_id=? AND run_id=? AND worker_id=? AND lease_expires_at>?""",
                    (
                        _iso(now + timedelta(seconds=ttl_seconds)),
                        _iso(now),
                        thread_id,
                        run_id,
                        worker_id,
                        _iso(now),
                    ),
                )
            return cursor.rowcount

        return await _run_in_thread(_op) == 1

    async def release(self, thread_id: str, run_id: str, worker_id: str) -> bool:
        def _op() -> int:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM agent_run_leases WHERE thread_id=? AND run_id=? AND worker_id=?",
                    (thread_id, run_id, worker_id),
                )
            return cursor.rowcount

        return await _run_in_thread(_op) == 1

    async def list_expired(self) -> list[RunLease]:
        def _op() -> list[Any]:
            with self._connect() as connection:
                return connection.execute(
                    """SELECT thread_id,run_id,worker_id,lease_expires_at,heartbeat_at
                    FROM agent_run_leases WHERE lease_expires_at<=?""",
                    (_iso(_now()),),
                ).fetchall()

        rows = await _run_in_thread(_op)
        return [RunLease(*row) for row in rows]


class PostgresRunLeaseStore(RunLeaseStore):
    """生产环境 PostgreSQL 租约实现。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any = None

    async def initialize(self) -> None:
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError("PostgresRunLeaseStore 需要 asyncpg") from exc
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as connection:
            await connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_run_leases (
                    thread_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    lease_expires_at TIMESTAMPTZ NOT NULL,
                    heartbeat_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (thread_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_run_leases_expiry
                ON agent_run_leases(lease_expires_at);"""
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresRunLeaseStore 未初始化")

    async def claim(
        self,
        thread_id: str,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        _validate_ttl(ttl_seconds)
        self._check()
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """INSERT INTO agent_run_leases
                (thread_id,run_id,worker_id,lease_expires_at,heartbeat_at)
                VALUES ($1,$2,$3,NOW()+($4::double precision * INTERVAL '1 second'),NOW())
                ON CONFLICT(thread_id,run_id) DO UPDATE SET
                worker_id=EXCLUDED.worker_id,
                lease_expires_at=EXCLUDED.lease_expires_at,
                heartbeat_at=EXCLUDED.heartbeat_at
                WHERE agent_run_leases.lease_expires_at<=NOW()
                   OR agent_run_leases.worker_id=EXCLUDED.worker_id
                RETURNING thread_id""",
                thread_id,
                run_id,
                worker_id,
                ttl_seconds,
            )
        return row is not None

    async def renew(
        self,
        thread_id: str,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        _validate_ttl(ttl_seconds)
        self._check()
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                """UPDATE agent_run_leases
                SET lease_expires_at=NOW()+($4::double precision * INTERVAL '1 second'),
                    heartbeat_at=NOW()
                WHERE thread_id=$1 AND run_id=$2 AND worker_id=$3
                  AND lease_expires_at>NOW()""",
                thread_id,
                run_id,
                worker_id,
                ttl_seconds,
            )
        return bool(result == "UPDATE 1")

    async def release(self, thread_id: str, run_id: str, worker_id: str) -> bool:
        self._check()
        async with self._pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM agent_run_leases WHERE thread_id=$1 AND run_id=$2 AND worker_id=$3",
                thread_id,
                run_id,
                worker_id,
            )
        return bool(result == "DELETE 1")

    async def list_expired(self) -> list[RunLease]:
        self._check()
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT thread_id,run_id,worker_id,lease_expires_at,heartbeat_at
                FROM agent_run_leases WHERE lease_expires_at<=NOW()"""
            )
        return [
            RunLease(
                thread_id=str(row["thread_id"]),
                run_id=str(row["run_id"]),
                worker_id=str(row["worker_id"]),
                lease_expires_at=_iso(_parse(row["lease_expires_at"])),
                heartbeat_at=_iso(_parse(row["heartbeat_at"])),
            )
            for row in rows
        ]


def _validate_ttl(ttl_seconds: float) -> None:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds 必须大于 0")


def create_run_lease_store(
    env: str = "development",
    *,
    sqlite_path: str = "./sessions.db",
    postgres_url: str | None = None,
) -> RunLeaseStore:
    """按环境创建 Run 租约存储。"""
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryRunLeaseStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境需要 PostgreSQL 连接地址")
        return PostgresRunLeaseStore(postgres_url)
    return SqliteRunLeaseStore(sqlite_path)


__all__ = [
    "MemoryRunLeaseStore",
    "PostgresRunLeaseStore",
    "RunLease",
    "RunLeaseStore",
    "SqliteRunLeaseStore",
    "create_run_lease_store",
]
