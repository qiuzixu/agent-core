"""Application-level governance storage for workflow retries and session events.

These stores are deliberately separate from LangGraph's checkpoint protocol.
LangGraph owns graph snapshots (thread_id / checkpoint_ns / checkpoint_id);
this module owns business-level node retries, side-effect idempotency and the
unified session event feed.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.storage._sqlite_utils import _run_in_thread
from agent_core.storage._sqlite_utils import ensure_wal as _ensure_wal


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class WorkflowIdempotencyRecord:
    key: str
    workflow_id: str = ""
    execution_id: str = ""
    node_id: str = ""
    arguments_hash: str = ""
    status: str = "running"
    result: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    error: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionEvent:
    thread_id: str
    event_type: str
    run_type: str = "workflow"
    run_id: str = ""
    parent_run_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkflowIdempotencyStore(ABC):
    @abstractmethod
    async def get(self, key: str) -> WorkflowIdempotencyRecord | None: ...

    @abstractmethod
    async def reserve(self, record: WorkflowIdempotencyRecord) -> WorkflowIdempotencyRecord | None:
        """Insert a key if absent; return the current owner otherwise."""
        ...

    @abstractmethod
    async def complete(self, key: str, *, result: dict[str, Any], message: str = "") -> None: ...

    @abstractmethod
    async def fail(self, key: str, *, error: str, message: str = "") -> None: ...


class MemoryWorkflowIdempotencyStore(WorkflowIdempotencyStore):
    def __init__(self) -> None:
        self._records: dict[str, WorkflowIdempotencyRecord] = {}

    async def get(self, key: str) -> WorkflowIdempotencyRecord | None:
        record = self._records.get(key)
        return WorkflowIdempotencyRecord(**record.to_dict()) if record else None

    async def reserve(self, record: WorkflowIdempotencyRecord) -> WorkflowIdempotencyRecord | None:
        current = self._records.get(record.key)
        if current is not None:
            return WorkflowIdempotencyRecord(**current.to_dict())
        copy = WorkflowIdempotencyRecord(**record.to_dict())
        self._records[copy.key] = copy
        return None

    async def complete(self, key: str, *, result: dict[str, Any], message: str = "") -> None:
        record = self._records.get(key)
        if record is None:
            return
        record.status = "completed"
        record.result = dict(result)
        record.message = message
        record.error = None
        record.updated_at = _now()

    async def fail(self, key: str, *, error: str, message: str = "") -> None:
        record = self._records.get(key)
        if record is None:
            return
        record.status = "failed"
        record.error = error
        record.message = message
        record.updated_at = _now()


class SqliteWorkflowIdempotencyStore(WorkflowIdempotencyStore):
    DDL = """
    CREATE TABLE IF NOT EXISTS workflow_idempotency_keys (
        key TEXT PRIMARY KEY,
        workflow_id TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        arguments_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        result_json TEXT NOT NULL DEFAULT '{}',
        message TEXT NOT NULL DEFAULT '',
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
            _ensure_wal(conn)
            conn.execute(self.DDL)

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> WorkflowIdempotencyRecord:
        (
            key,
            workflow_id,
            execution_id,
            node_id,
            arguments_hash,
            status,
            result_json,
            message,
            error,
            created_at,
            updated_at,
        ) = row
        return WorkflowIdempotencyRecord(
            key=key,
            workflow_id=workflow_id,
            execution_id=execution_id,
            node_id=node_id,
            arguments_hash=arguments_hash,
            status=status,
            result=json.loads(result_json) if result_json else {},
            message=message,
            error=error,
            created_at=created_at,
            updated_at=updated_at,
        )

    async def get(self, key: str) -> WorkflowIdempotencyRecord | None:
        def op() -> tuple[Any, ...] | None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                row = conn.execute(
                    "SELECT * FROM workflow_idempotency_keys WHERE key=?",
                    (key,),
                ).fetchone()
                return None if row is None else tuple(row)

        row = await _run_in_thread(op)
        return self._from_row(row) if row else None

    async def reserve(self, record: WorkflowIdempotencyRecord) -> WorkflowIdempotencyRecord | None:
        payload = record.to_dict()

        def op() -> WorkflowIdempotencyRecord | None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM workflow_idempotency_keys WHERE key=?", (record.key,)
                ).fetchone()
                if row is not None:
                    return self._from_row(row)
                conn.execute(
                    """
                    INSERT INTO workflow_idempotency_keys
                        (key, workflow_id, execution_id, node_id, arguments_hash,
                         status, result_json, message, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.key,
                        record.workflow_id,
                        record.execution_id,
                        record.node_id,
                        record.arguments_hash,
                        record.status,
                        json.dumps(payload.get("result") or {}, ensure_ascii=False),
                        record.message,
                        record.error,
                        record.created_at,
                        record.updated_at,
                    ),
                )
                return None

        return await _run_in_thread(op)

    async def complete(self, key: str, *, result: dict[str, Any], message: str = "") -> None:
        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    """UPDATE workflow_idempotency_keys
                       SET status='completed', result_json=?, message=?, error=NULL, updated_at=?
                       WHERE key=?""",
                    (json.dumps(result, ensure_ascii=False), message, _now(), key),
                )

        await _run_in_thread(op)

    async def fail(self, key: str, *, error: str, message: str = "") -> None:
        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    """UPDATE workflow_idempotency_keys
                       SET status='failed', error=?, message=?, updated_at=?
                       WHERE key=?""",
                    (error, message, _now(), key),
                )

        await _run_in_thread(op)


class PostgresWorkflowIdempotencyStore(WorkflowIdempotencyStore):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any | None = None

    async def initialize(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS workflow_idempotency_keys (
                key TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                result JSONB NOT NULL DEFAULT '{}'::jsonb,
                message TEXT NOT NULL DEFAULT '',
                error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresWorkflowIdempotencyStore 未初始化")

    async def get(self, key: str) -> WorkflowIdempotencyRecord | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow("SELECT * FROM workflow_idempotency_keys WHERE key=$1", key)
        if row is None:
            return None
        return WorkflowIdempotencyRecord(
            key=str(row["key"]),
            workflow_id=str(row["workflow_id"]),
            execution_id=str(row["execution_id"]),
            node_id=str(row["node_id"]),
            arguments_hash=str(row["arguments_hash"]),
            status=str(row["status"]),
            result=dict(row["result"] or {}),
            message=str(row["message"] or ""),
            error=row["error"],
            created_at=row["created_at"].isoformat(),
            updated_at=row["updated_at"].isoformat(),
        )

    async def reserve(self, record: WorkflowIdempotencyRecord) -> WorkflowIdempotencyRecord | None:
        self._check()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM workflow_idempotency_keys WHERE key=$1 FOR UPDATE",
                    record.key,
                )
                if row is not None:
                    return await self.get(record.key)
                await conn.execute(
                    """
                    INSERT INTO workflow_idempotency_keys
                        (key, workflow_id, execution_id, node_id, arguments_hash,
                         status, result, message, error, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now(), now())
                    """,
                    record.key,
                    record.workflow_id,
                    record.execution_id,
                    record.node_id,
                    record.arguments_hash,
                    record.status,
                    record.result or {},
                    record.message,
                    record.error,
                )
                return None

    async def complete(self, key: str, *, result: dict[str, Any], message: str = "") -> None:
        self._check()
        assert self._pool is not None
        await self._pool.execute(
            """UPDATE workflow_idempotency_keys
               SET status='completed', result=$2, message=$3, error=NULL, updated_at=now()
               WHERE key=$4""",
            result,
            message,
            key,
        )

    async def fail(self, key: str, *, error: str, message: str = "") -> None:
        self._check()
        assert self._pool is not None
        await self._pool.execute(
            """UPDATE workflow_idempotency_keys
               SET status='failed', error=$2, message=$3, updated_at=now()
               WHERE key=$4""",
            error,
            message,
            key,
        )


class SessionEventStore(ABC):
    @abstractmethod
    async def append(self, event: SessionEvent) -> SessionEvent: ...

    @abstractmethod
    async def list(
        self,
        thread_id: str,
        *,
        run_type: str | None = None,
        limit: int = 200,
    ) -> list[SessionEvent]: ...


class MemorySessionEventStore(SessionEventStore):
    def __init__(self) -> None:
        self._events: dict[str, SessionEvent] = {}

    async def append(self, event: SessionEvent) -> SessionEvent:
        self._events[event.event_id] = SessionEvent(**event.to_dict())
        return event

    async def list(
        self,
        thread_id: str,
        *,
        run_type: str | None = None,
        limit: int = 200,
    ) -> list[SessionEvent]:
        events = [
            event
            for event in self._events.values()
            if event.thread_id == thread_id and (run_type is None or event.run_type == run_type)
        ]
        return sorted(events, key=lambda item: item.created_at)[:limit]


class SqliteSessionEventStore(SessionEventStore):
    DDL = """
    CREATE TABLE IF NOT EXISTS session_events (
        event_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        run_type TEXT NOT NULL,
        run_id TEXT NOT NULL,
        parent_run_id TEXT,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
            _ensure_wal(conn)
            conn.execute(self.DDL)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_events_thread "
                "ON session_events(thread_id, created_at)"
            )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> SessionEvent:
        return SessionEvent(
            event_id=str(row[0]),
            thread_id=str(row[1]),
            run_type=str(row[2]),
            run_id=str(row[3]),
            parent_run_id=row[4],
            event_type=str(row[5]),
            payload=json.loads(row[6] or "{}"),
            created_at=str(row[7]),
        )

    async def append(self, event: SessionEvent) -> SessionEvent:
        value = event.to_dict()

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO session_events
                       (event_id, thread_id, run_type, run_id, parent_run_id,
                        event_type, payload_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id,
                        event.thread_id,
                        event.run_type,
                        event.run_id,
                        event.parent_run_id,
                        event.event_type,
                        json.dumps(value["payload"], ensure_ascii=False),
                        event.created_at,
                    ),
                )

        await _run_in_thread(op)
        return event

    async def list(
        self,
        thread_id: str,
        *,
        run_type: str | None = None,
        limit: int = 200,
    ) -> list[SessionEvent]:
        query = "SELECT * FROM session_events WHERE thread_id=?"
        args: list[Any] = [thread_id]
        if run_type is not None:
            query += " AND run_type=?"
            args.append(run_type)
        query += " ORDER BY created_at LIMIT ?"
        args.append(limit)

        def op() -> list[tuple[Any, ...]]:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                return conn.execute(query, args).fetchall()

        rows = await _run_in_thread(op)
        return [self._from_row(row) for row in rows]


class PostgresSessionEventStore(SessionEventStore):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any | None = None

    async def initialize(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        await self._pool.execute(
            """
            CREATE TABLE IF NOT EXISTS session_events (
                event_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                run_type TEXT NOT NULL,
                run_id TEXT NOT NULL,
                parent_run_id TEXT,
                event_type TEXT NOT NULL,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await self._pool.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_events_thread ON session_events(thread_id, created_at)"
        )

    async def append(self, event: SessionEvent) -> SessionEvent:
        self._check()
        assert self._pool is not None
        await self._pool.execute(
            """INSERT INTO session_events
               (event_id, thread_id, run_type, run_id, parent_run_id, event_type, payload, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, now())
               ON CONFLICT (event_id) DO NOTHING""",
            event.event_id,
            event.thread_id,
            event.run_type,
            event.run_id,
            event.parent_run_id,
            event.event_type,
            event.payload,
        )
        return event

    async def list(
        self,
        thread_id: str,
        *,
        run_type: str | None = None,
        limit: int = 200,
    ) -> list[SessionEvent]:
        self._check()
        assert self._pool is not None
        if run_type is None:
            rows = await self._pool.fetch(
                """SELECT * FROM session_events WHERE thread_id=$1
                   ORDER BY created_at LIMIT $2""",
                thread_id,
                limit,
            )
        else:
            rows = await self._pool.fetch(
                """SELECT * FROM session_events WHERE thread_id=$1 AND run_type=$2
                   ORDER BY created_at LIMIT $3""",
                thread_id,
                run_type,
                limit,
            )
        return [
            SessionEvent(
                event_id=str(row["event_id"]),
                thread_id=str(row["thread_id"]),
                run_type=str(row["run_type"]),
                run_id=str(row["run_id"]),
                parent_run_id=row["parent_run_id"],
                event_type=str(row["event_type"]),
                payload=dict(row["payload"] or {}),
                created_at=row["created_at"].isoformat(),
            )
            for row in rows
        ]

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresSessionEventStore 未初始化")


def create_workflow_idempotency_store(
    env: str = "development",
    *,
    sqlite_path: str | Path,
    postgres_url: str | None = None,
) -> WorkflowIdempotencyStore:
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryWorkflowIdempotencyStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境幂等存储需要 PostgreSQL")
        return PostgresWorkflowIdempotencyStore(postgres_url)
    return SqliteWorkflowIdempotencyStore(sqlite_path)


def create_session_event_store(
    env: str = "development",
    *,
    sqlite_path: str | Path,
    postgres_url: str | None = None,
) -> SessionEventStore:
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemorySessionEventStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境会话事件存储需要 PostgreSQL")
        return PostgresSessionEventStore(postgres_url)
    return SqliteSessionEventStore(sqlite_path)


__all__ = [
    "MemorySessionEventStore",
    "MemoryWorkflowIdempotencyStore",
    "PostgresSessionEventStore",
    "PostgresWorkflowIdempotencyStore",
    "SessionEvent",
    "SessionEventStore",
    "SqliteSessionEventStore",
    "SqliteWorkflowIdempotencyStore",
    "WorkflowIdempotencyRecord",
    "WorkflowIdempotencyStore",
    "create_session_event_store",
    "create_workflow_idempotency_store",
]
