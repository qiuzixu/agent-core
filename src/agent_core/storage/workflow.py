"""工作流和宏执行实例存储。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.storage._sqlite_utils import (
    _run_in_thread,
    run_sync_in_thread,
    sqlite_connection,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorkflowConcurrencyError(RuntimeError):
    """工作流执行实例版本冲突。"""


def _json_value(value: Any, default: dict[str, Any] | list[dict[str, Any]]) -> Any:
    """统一 SQLite 文本和 asyncpg JSONB 的返回形态。"""
    if value is None or value == "":
        return default
    return json.loads(value) if isinstance(value, str) else value


def _postgres_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass
class WorkflowExecution:
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    definition_id: str = ""
    execution_type: str = "workflow"
    user_id: str | None = None
    tenant_id: str | None = None
    status: str = "queued"
    current_step: str | None = None
    steps_completed: int = 0
    total_steps: int = 0
    input_data: dict[str, Any] = field(default_factory=dict)
    result_data: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    version: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "definition_id": self.definition_id,
            "execution_type": self.execution_type,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "current_step": self.current_step,
            "steps_completed": self.steps_completed,
            "total_steps": self.total_steps,
            "input_data": self.input_data,
            "result_data": self.result_data,
            "events": self.events,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WorkflowExecutionStore(ABC):
    @abstractmethod
    async def save(self, execution: WorkflowExecution) -> None: ...

    @abstractmethod
    async def load(self, execution_id: str) -> WorkflowExecution | None: ...

    @abstractmethod
    async def list(self, definition_id: str, tenant_id: str | None = None) -> list[WorkflowExecution]: ...

    # 统一端口名称，同时保留工作流模块已有的 save/load/list 调用。
    async def save_execution(self, execution: WorkflowExecution) -> None:
        await self.save(execution)

    async def load_execution(self, execution_id: str) -> WorkflowExecution | None:
        return await self.load(execution_id)


def _from_dict(value: dict[str, Any]) -> WorkflowExecution:
    input_data = _json_value(value.get("input_data"), {})
    result_data = _json_value(value.get("result_data"), {})
    events = _json_value(value.get("events"), [])
    return WorkflowExecution(
        execution_id=str(value["execution_id"]),
        definition_id=str(value.get("definition_id", "")),
        execution_type=str(value.get("execution_type", "workflow")),
        user_id=value.get("user_id"),
        tenant_id=value.get("tenant_id"),
        status=str(value.get("status", "queued")),
        current_step=value.get("current_step"),
        steps_completed=int(value.get("steps_completed", 0)),
        total_steps=int(value.get("total_steps", 0)),
        input_data=dict(input_data),
        result_data=dict(result_data),
        events=list(events),
        version=int(value.get("version", 0)),
        created_at=str(value.get("created_at", _now())),
        updated_at=str(value.get("updated_at", _now())),
    )


class MemoryWorkflowExecutionStore(WorkflowExecutionStore):
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def save(self, execution: WorkflowExecution) -> None:
        async with self._lock:
            previous = self._items.get(execution.execution_id)
            stored_version = int(previous.get("version", 0)) if previous else 0
            if previous is not None and stored_version != execution.version:
                raise WorkflowConcurrencyError(f"工作流 {execution.execution_id} 版本冲突")
            execution.version = stored_version + 1
            execution.updated_at = _now()
            self._items[execution.execution_id] = execution.to_dict()

    async def load(self, execution_id: str) -> WorkflowExecution | None:
        value = self._items.get(execution_id)
        return _from_dict(value) if value else None

    async def list(self, definition_id: str, tenant_id: str | None = None) -> list[WorkflowExecution]:
        return [
            _from_dict(value)
            for value in self._items.values()
            if value.get("definition_id") == definition_id
            and (tenant_id is None or value.get("tenant_id") == tenant_id)
        ]


class SqliteWorkflowExecutionStore(WorkflowExecutionStore):
    DDL = """
    CREATE TABLE IF NOT EXISTS workflow_executions (
        execution_id TEXT PRIMARY KEY,
        definition_id TEXT NOT NULL,
        execution_type TEXT NOT NULL,
        user_id TEXT,
        tenant_id TEXT,
        status TEXT NOT NULL,
        current_step TEXT,
        steps_completed INTEGER NOT NULL DEFAULT 0,
        total_steps INTEGER NOT NULL DEFAULT 0,
        input_data TEXT NOT NULL DEFAULT '{}',
        result_data TEXT NOT NULL DEFAULT '{}',
        events TEXT NOT NULL DEFAULT '[]',
        version INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_workflow_execution_definition
    ON workflow_executions(definition_id, tenant_id, updated_at);
    """

    def __init__(self, db_path: str | Path = "./sessions.db") -> None:
        self._db_path = str(Path(db_path).resolve())
        # 建表 DDL/PRAGMA 初始化同样入线程，避免构造期冻结事件循环。
        run_sync_in_thread(self._init_db)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(self.DDL)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """获取 SQLite 连接（同步，自动提交/回滚；统一 timeout 与 WAL 规范）。"""
        with sqlite_connection(self._db_path) as conn:
            yield conn

    @staticmethod
    def _row(row: tuple[Any, ...]) -> WorkflowExecution:
        keys = [
            "execution_id",
            "definition_id",
            "execution_type",
            "user_id",
            "tenant_id",
            "status",
            "current_step",
            "steps_completed",
            "total_steps",
            "input_data",
            "result_data",
            "events",
            "version",
            "created_at",
            "updated_at",
        ]
        value = dict(zip(keys, row, strict=True))
        for key in ("input_data", "result_data", "events"):
            value[key] = json.loads(value[key]) if value[key] else ({} if key != "events" else [])
        return _from_dict(value)

    async def save(self, execution: WorkflowExecution) -> None:
        def _op() -> tuple[int, str]:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT version FROM workflow_executions WHERE execution_id=?",
                    (execution.execution_id,),
                ).fetchone()
                stored_version = int(row[0]) if row else 0
                if row is not None and stored_version != execution.version:
                    raise WorkflowConcurrencyError(f"工作流 {execution.execution_id} 版本冲突")
                next_version = stored_version + 1
                updated_at = _now()
                value = execution.to_dict()
                cursor = conn.execute(
                    """INSERT INTO workflow_executions
                    (execution_id,definition_id,execution_type,user_id,tenant_id,status,current_step,
                     steps_completed,total_steps,input_data,result_data,events,version,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(execution_id) DO UPDATE SET status=excluded.status,
                    current_step=excluded.current_step, steps_completed=excluded.steps_completed,
                    total_steps=excluded.total_steps, result_data=excluded.result_data,
                    events=excluded.events, version=excluded.version, updated_at=excluded.updated_at
                    WHERE workflow_executions.version=?""",
                    (
                        execution.execution_id,
                        execution.definition_id,
                        execution.execution_type,
                        execution.user_id,
                        execution.tenant_id,
                        execution.status,
                        execution.current_step,
                        execution.steps_completed,
                        execution.total_steps,
                        json.dumps(value["input_data"], ensure_ascii=False),
                        json.dumps(value["result_data"], ensure_ascii=False),
                        json.dumps(value["events"], ensure_ascii=False),
                        next_version,
                        execution.created_at,
                        updated_at,
                        stored_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise WorkflowConcurrencyError(f"工作流 {execution.execution_id} 保存时发生并发冲突")
            return next_version, updated_at

        next_version, updated_at = await _run_in_thread(_op)
        execution.version = next_version
        execution.updated_at = updated_at

    async def load(self, execution_id: str) -> WorkflowExecution | None:
        def _op() -> Any:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT execution_id,definition_id,execution_type,user_id,tenant_id,status,current_step,"
                    "steps_completed,total_steps,input_data,result_data,events,version,created_at,updated_at "
                    "FROM workflow_executions WHERE execution_id=?",
                    (execution_id,),
                ).fetchone()

        row = await _run_in_thread(_op)
        return self._row(row) if row else None

    async def list(self, definition_id: str, tenant_id: str | None = None) -> list[WorkflowExecution]:
        query = (
            "SELECT execution_id,definition_id,execution_type,user_id,tenant_id,"
            "status,current_step,steps_completed,total_steps,input_data,result_data,"
            "events,version,created_at,updated_at FROM workflow_executions "
            "WHERE definition_id=?"
        )
        args: list[Any] = [definition_id]
        if tenant_id is not None:
            query += " AND tenant_id=?"
            args.append(tenant_id)
        query += " ORDER BY updated_at DESC"

        def _op() -> list[Any]:
            with self._connect() as conn:
                return conn.execute(query, args).fetchall()

        rows = await _run_in_thread(_op)
        return [self._row(row) for row in rows]


class PostgresWorkflowExecutionStore(WorkflowExecutionStore):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any = None

    async def initialize(self) -> None:
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError("PostgresWorkflowExecutionStore 需要 asyncpg") from exc
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_executions (
                    execution_id TEXT PRIMARY KEY, definition_id TEXT NOT NULL,
                    execution_type TEXT NOT NULL, user_id TEXT, tenant_id TEXT,
                    status TEXT NOT NULL, current_step TEXT,
                    steps_completed INTEGER NOT NULL DEFAULT 0, total_steps INTEGER NOT NULL DEFAULT 0,
                    input_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    result_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    events JSONB NOT NULL DEFAULT '[]'::jsonb,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
                )
            """)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresWorkflowExecutionStore 未初始化")

    async def save(self, execution: WorkflowExecution) -> None:
        self._check()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchval(
                    "SELECT version FROM workflow_executions WHERE execution_id=$1 FOR UPDATE",
                    execution.execution_id,
                )
                stored_version = int(previous) if previous is not None else 0
                if previous is not None and stored_version != execution.version:
                    raise WorkflowConcurrencyError(f"工作流 {execution.execution_id} 版本冲突")
                next_version = stored_version + 1
                updated_at = _now()
                value = execution.to_dict()
                command = await conn.execute(
                    """INSERT INTO workflow_executions
                    (execution_id,definition_id,execution_type,user_id,tenant_id,status,
                     current_step,steps_completed,total_steps,input_data,result_data,
                     events,version,created_at,updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12::jsonb,$13,$14,$15)
                    ON CONFLICT(execution_id) DO UPDATE SET
                        status=$6,current_step=$7,steps_completed=$8,total_steps=$9,
                        result_data=$11::jsonb,events=$12::jsonb,version=$13,updated_at=$15
                    WHERE workflow_executions.version=$16""",
                    execution.execution_id,
                    execution.definition_id,
                    execution.execution_type,
                    execution.user_id,
                    execution.tenant_id,
                    execution.status,
                    execution.current_step,
                    execution.steps_completed,
                    execution.total_steps,
                    json.dumps(value["input_data"]),
                    json.dumps(value["result_data"]),
                    json.dumps(value["events"]),
                    next_version,
                    _postgres_datetime(execution.created_at),
                    _postgres_datetime(updated_at),
                    stored_version,
                )
                expected = "INSERT 0 1" if previous is None else "UPDATE 1"
                if command != expected:
                    raise WorkflowConcurrencyError(f"工作流 {execution.execution_id} 保存时发生并发冲突")
            execution.version = next_version
            execution.updated_at = updated_at

    async def load(self, execution_id: str) -> WorkflowExecution | None:
        self._check()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM workflow_executions WHERE execution_id=$1", execution_id)
        return _from_dict(dict(row)) if row else None

    async def list(self, definition_id: str, tenant_id: str | None = None) -> list[WorkflowExecution]:
        self._check()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM workflow_executions
                WHERE definition_id=$1 AND ($2::text IS NULL OR tenant_id=$2)
                ORDER BY updated_at DESC""",
                definition_id,
                tenant_id,
            )
        return [_from_dict(dict(row)) for row in rows]


def create_workflow_execution_store(
    env: str = "development",
    *,
    sqlite_path: str = "./sessions.db",
    postgres_url: str | None = None,
) -> WorkflowExecutionStore:
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryWorkflowExecutionStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境需要 PostgreSQL 连接地址")
        return PostgresWorkflowExecutionStore(postgres_url)
    return SqliteWorkflowExecutionStore(sqlite_path)
