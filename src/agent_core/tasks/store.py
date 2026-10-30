"""持久化的应用级 Agent 任务存储。

任务信封生命周期（queued / running / waiting_retry / completed / dead 等）
由本模块管理，与 agent-core checkpoint（管理图/消息执行状态）职责分离。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_core.storage._sqlite_utils import _run_in_thread
from agent_core.storage._sqlite_utils import ensure_wal as _ensure_wal


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass
class AgentTask:
    """一条 RabbitMQ Agent 任务的持久化视图。"""

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    intent: str = ""
    status: str = "queued"
    queue: str = "agent.task.normal"
    thread_id: str | None = None
    user_id: str = "anonymous"
    tenant_id: str = "default"
    idempotency_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    worker_id: str | None = None
    next_retry_at: str | None = None
    workflow_execution_id: str | None = None
    attempt_id: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    lease_expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentTaskStore(ABC):
    @abstractmethod
    async def create(self, task: AgentTask) -> AgentTask: ...

    @abstractmethod
    async def get(self, task_id: str) -> AgentTask | None: ...

    @abstractmethod
    async def get_by_idempotency_key(self, key: str) -> AgentTask | None: ...

    @abstractmethod
    async def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
        attempt_id: str | None = None,
    ) -> AgentTask | None:
        """将 queued/waiting_retry 状态原子转为 running，分配给一个 Worker。"""

    @abstractmethod
    async def set_attempt(
        self,
        task_id: str,
        attempt_id: str,
        *,
        retry_count: int | None = None,
    ) -> AgentTask | None:
        """原子发布 Worker 唯一可执行的 attempt_id。"""

    @abstractmethod
    async def set_waiting_retry(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
    ) -> AgentTask | None:
        """传输层失败后将任务回退到 retry 调度。"""

    @abstractmethod
    async def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
    ) -> AgentTask | None: ...

    @abstractmethod
    async def renew_leases(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> int: ...

    @abstractmethod
    async def reclaim_stale(
        self,
        *,
        worker_id: str = "lease-reaper",
        limit: int = 100,
    ) -> list[AgentTask]: ...

    @abstractmethod
    async def complete(
        self,
        task_id: str,
        *,
        worker_id: str,
        result: dict[str, Any] | None,
    ) -> AgentTask | None: ...

    @abstractmethod
    async def fail(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
    ) -> AgentTask | None:
        """递增重试计数；还有配额则进入 waiting_retry，否则标记失败。"""

    @abstractmethod
    async def mark_dead(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
    ) -> AgentTask | None: ...

    @abstractmethod
    async def mark_requeued(
        self,
        task_id: str,
        *,
        worker_id: str,
    ) -> AgentTask | None:
        """将 dead 任务手动回放为 queued。"""

    @abstractmethod
    async def mark_discarded(
        self,
        task_id: str,
        *,
        worker_id: str,
        reason: str,
    ) -> AgentTask | None:
        """运维确认后将 dead 任务标记为 discarded。"""

    @abstractmethod
    async def reschedule(self, task_id: str, *, worker_id: str = "scheduler") -> AgentTask | None:
        """将 waiting_retry 原子转回 queued；只有一个调度器能成功。"""

    @abstractmethod
    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
    ) -> list[AgentTask]: ...

    @abstractmethod
    async def list_tasks(
        self,
        *,
        status: str | None = None,
        intent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        worker_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        """按创建时间返回任务列表，支持可选过滤。"""
        ...


class MemoryAgentTaskStore(AgentTaskStore):
    def __init__(self) -> None:
        self._tasks: dict[str, AgentTask] = {}
        self._lock = asyncio.Lock()

    async def create(self, task: AgentTask) -> AgentTask:
        async with self._lock:
            if task.idempotency_key:
                for existing in self._tasks.values():
                    if existing.idempotency_key == task.idempotency_key:
                        return existing
            self._tasks[task.task_id] = AgentTask(**task.to_dict())
            return AgentTask(**task.to_dict())

    async def get(self, task_id: str) -> AgentTask | None:
        task = self._tasks.get(task_id)
        return AgentTask(**task.to_dict()) if task else None

    async def get_by_idempotency_key(self, key: str) -> AgentTask | None:
        for task in self._tasks.values():
            if task.idempotency_key == key:
                return AgentTask(**task.to_dict())
        return None

    async def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
        attempt_id: str | None = None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            now = datetime.now(UTC)
            if task is not None and attempt_id is not None and task.attempt_id != attempt_id:
                return None
            lease_expired = (
                task is not None
                and task.status == "running"
                and (
                    task.lease_expires_at is None
                    or _parse_time(task.lease_expires_at) <= now
                )
            )
            if task is None or (
                task.status not in {"queued", "waiting_retry"} and not lease_expired
            ):
                return None
            task.status = "running"
            task.worker_id = worker_id
            task.error = None
            task.updated_at = _now()
            task.lease_expires_at = (
                now + timedelta(seconds=max(0.1, lease_seconds))
            ).isoformat()
            return AgentTask(**task.to_dict())

    async def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "running" or task.worker_id != worker_id:
                return None
            task.lease_expires_at = (
                datetime.now(UTC) + timedelta(seconds=max(0.1, lease_seconds))
            ).isoformat()
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def renew_leases(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> int:
        async with self._lock:
            renewed = 0
            for task in self._tasks.values():
                if task.status != "running" or task.worker_id != worker_id:
                    continue
                task.lease_expires_at = (
                    datetime.now(UTC) + timedelta(seconds=max(0.1, lease_seconds))
                ).isoformat()
                task.updated_at = _now()
                renewed += 1
            return renewed

    async def reclaim_stale(
        self,
        *,
        worker_id: str = "lease-reaper",
        limit: int = 100,
    ) -> list[AgentTask]:
        async with self._lock:
            now = datetime.now(UTC)
            reclaimed: list[AgentTask] = []
            for task in self._tasks.values():
                if len(reclaimed) >= limit:
                    break
                expired = task.status == "running" and (
                    task.lease_expires_at is None
                    or _parse_time(task.lease_expires_at) <= now
                )
                if not expired:
                    continue
                task.status = "waiting_retry"
                task.worker_id = worker_id
                task.next_retry_at = now.isoformat()
                task.updated_at = _now()
                reclaimed.append(AgentTask(**task.to_dict()))
            return reclaimed

    async def complete(
        self,
        task_id: str,
        *,
        worker_id: str,
        result: dict[str, Any] | None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "running" or task.worker_id != worker_id:
                return None
            task.status = "completed"
            task.result = result
            task.error = None
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def fail(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "running" or task.worker_id != worker_id:
                return None
            task.retry_count += 1
            task.error = error
            task.updated_at = _now()
            if task.retry_count < task.max_retries:
                task.status = "waiting_retry"
                task.next_retry_at = (
                    datetime.now(UTC) + timedelta(seconds=max(0.1, retry_delay_seconds))
                ).isoformat()
            else:
                task.status = "failed"
                task.next_retry_at = None
            return AgentTask(**task.to_dict())

    async def mark_dead(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.status = "dead"
            task.error = error
            task.worker_id = worker_id
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def mark_requeued(
        self,
        task_id: str,
        *,
        worker_id: str,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "dead":
                return None
            task.status = "queued"
            task.worker_id = worker_id
            task.error = None
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())


    async def mark_discarded(
        self,
        task_id: str,
        *,
        worker_id: str,
        reason: str,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "dead":
                return None
            task.status = "discarded"
            task.worker_id = worker_id
            task.error = reason
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())


    async def set_attempt(
        self,
        task_id: str,
        attempt_id: str,
        *,
        retry_count: int | None = None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status not in {"queued", "waiting_retry"}:
                return None
            task.attempt_id = attempt_id
            if retry_count is not None:
                task.retry_count = max(0, int(retry_count))
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def set_waiting_retry(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.status = "waiting_retry"
            task.worker_id = worker_id
            task.error = error
            task.next_retry_at = (
                datetime.now(UTC) + timedelta(seconds=max(0.1, retry_delay_seconds))
            ).isoformat()
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def reschedule(self, task_id: str, *, worker_id: str = "scheduler") -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "waiting_retry":
                return None
            task.status = "queued"
            task.worker_id = worker_id
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
    ) -> list[AgentTask]:
        return [
            AgentTask(**task.to_dict())
            for task in self._tasks.values()
            if task.status == status
        ][:limit]

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        intent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        worker_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        tasks = [
            AgentTask(**task.to_dict())
            for task in self._tasks.values()
            if (status is None or task.status == status)
            and (intent is None or task.intent == intent)
            and (tenant_id is None or task.tenant_id == tenant_id)
            and (user_id is None or task.user_id == user_id)
            and (worker_id is None or task.worker_id == worker_id)
        ]
        tasks.sort(key=lambda task: task.created_at)
        return tasks[:max(1, int(limit))]


def _task_from_row(value: Any) -> AgentTask:
    if not isinstance(value, (tuple, list)):
        raise TypeError("unexpected database row type")
    (
        task_id,
        intent,
        status,
        queue,
        thread_id,
        user_id,
        tenant_id,
        idempotency_key,
        payload_json,
        result_json,
        error,
        retry_count,
        max_retries,
        worker_id,
        next_retry_at,
        workflow_execution_id,
        created_at,
        updated_at,
        lease_expires_at,
        attempt_id,
    ) = value
    return AgentTask(
        task_id=str(task_id),
        intent=str(intent),
        status=str(status),
        queue=str(queue),
        thread_id=thread_id,
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        idempotency_key=idempotency_key,
        payload=json.loads(payload_json or "{}"),
        result=json.loads(result_json) if result_json is not None else None,
        error=error,
        retry_count=int(retry_count),
        max_retries=int(max_retries),
        worker_id=worker_id,
        next_retry_at=next_retry_at.isoformat() if hasattr(next_retry_at, "isoformat") else next_retry_at,
        workflow_execution_id=workflow_execution_id,
        created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        updated_at=updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at),
        lease_expires_at=(
            lease_expires_at.isoformat()
            if hasattr(lease_expires_at, "isoformat")
            else lease_expires_at
        ),
        attempt_id=attempt_id,
    )


class SqliteAgentTaskStore(AgentTaskStore):
    DDL = """
    CREATE TABLE IF NOT EXISTS agent_tasks (
        task_id TEXT PRIMARY KEY,
        intent TEXT NOT NULL,
        status TEXT NOT NULL,
        queue TEXT NOT NULL,
        thread_id TEXT,
        user_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        idempotency_key TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        result_json TEXT,
        error TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        worker_id TEXT,
        next_retry_at TEXT,
        workflow_execution_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        lease_expires_at TEXT,
        attempt_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_retry
        ON agent_tasks(status, next_retry_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_idempotency
        ON agent_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
    """

    _COLUMNS = (
        "task_id, intent, status, queue, thread_id, user_id, tenant_id, idempotency_key, "
        "payload_json, result_json, error, retry_count, max_retries, worker_id, "
        "next_retry_at, workflow_execution_id, created_at, updated_at, lease_expires_at, attempt_id"
    )

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
            _ensure_wal(conn)
            conn.executescript(self.DDL)
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(agent_tasks)")
            }
            for column, ddl in (
                ("lease_expires_at", "ALTER TABLE agent_tasks ADD COLUMN lease_expires_at TEXT"),
                ("attempt_id", "ALTER TABLE agent_tasks ADD COLUMN attempt_id TEXT"),
            ):
                if column not in columns:
                    conn.execute(ddl)


    @staticmethod
    def _values(task: AgentTask) -> tuple[Any, ...]:
        return (
            task.task_id,
            task.intent,
            task.status,
            task.queue,
            task.thread_id,
            task.user_id,
            task.tenant_id,
            task.idempotency_key,
            json.dumps(task.payload, ensure_ascii=False),
            json.dumps(task.result, ensure_ascii=False) if task.result is not None else None,
            task.error,
            task.retry_count,
            task.max_retries,
            task.worker_id,
            task.next_retry_at,
            task.workflow_execution_id,
            task.created_at,
            task.updated_at,
            task.lease_expires_at,
            task.attempt_id,
        )

    async def create(self, task: AgentTask) -> AgentTask:
        values = self._values(task)

        def op() -> bool:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                try:
                    conn.execute(
                        f"INSERT INTO agent_tasks ({self._COLUMNS}) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        values,
                    )
                    conn.commit()
                    return False
                except sqlite3.IntegrityError:
                    row = conn.execute(
                        "SELECT idempotency_key FROM agent_tasks WHERE idempotency_key = ?",
                        (task.idempotency_key,),
                    ).fetchone()
                    return row is not None

        conflict = await _run_in_thread(op)
        if conflict and task.idempotency_key:
            return await self.get_by_idempotency_key(task.idempotency_key) or task
        return task

    async def get(self, task_id: str) -> AgentTask | None:
        def op() -> Any:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                return conn.execute(
                    f"SELECT {self._COLUMNS} FROM agent_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()

        row = await _run_in_thread(op)
        return _task_from_row(row) if row is not None else None

    async def get_by_idempotency_key(self, key: str) -> AgentTask | None:
        def op() -> Any:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                return conn.execute(
                    f"SELECT {self._COLUMNS} FROM agent_tasks WHERE idempotency_key = ?",
                    (key,),
                ).fetchone()

        row = await _run_in_thread(op)
        return _task_from_row(row) if row is not None else None

    async def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
        attempt_id: str | None = None,
    ) -> AgentTask | None:
        now = _now()
        lease_expires_at = (
            datetime.now(UTC) + timedelta(seconds=max(0.1, lease_seconds))
        ).isoformat()
        current = await self.get(task_id)
        if current is not None and attempt_id is not None and current.attempt_id != attempt_id:
            return None

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status='running', worker_id=?, error=NULL, "
                    "updated_at=?, lease_expires_at=? WHERE task_id=? AND "
                    "(? IS NULL OR attempt_id=?) AND ("
                    "status IN ('queued','waiting_retry') OR (status='running' AND "
                    "(lease_expires_at IS NULL OR lease_expires_at <= ?)))",
                    (worker_id, now, lease_expires_at, task_id, attempt_id, attempt_id, now),
                )
                conn.commit()

        await _run_in_thread(op)
        task = await self.get(task_id)
        if task is not None and task.status == "running" and task.worker_id == worker_id:
            return task
        return None

    async def set_attempt(
        self,
        task_id: str,
        attempt_id: str,
        *,
        retry_count: int | None = None,
    ) -> AgentTask | None:
        now = _now()

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                if retry_count is None:
                    conn.execute(
                        "UPDATE agent_tasks SET attempt_id=?, updated_at=? "
                        "WHERE task_id=? AND status IN ('queued','waiting_retry')",
                        (attempt_id, now, task_id),
                    )
                else:
                    conn.execute(
                        "UPDATE agent_tasks SET attempt_id=?, retry_count=?, updated_at=? "
                        "WHERE task_id=? AND status IN ('queued','waiting_retry')",
                        (attempt_id, max(0, int(retry_count)), now, task_id),
                    )
                conn.commit()

        await _run_in_thread(op)
        task = await self.get(task_id)
        if task is not None and task.attempt_id == attempt_id:
            return task
        return None

    async def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
    ) -> AgentTask | None:
        now = _now()
        lease_expires_at = (
            datetime.now(UTC) + timedelta(seconds=max(0.1, lease_seconds))
        ).isoformat()

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET lease_expires_at=?, updated_at=? "
                    "WHERE task_id=? AND status='running' AND worker_id=?",
                    (lease_expires_at, now, task_id, worker_id),
                )
                conn.commit()

        await _run_in_thread(op)
        return await self.get(task_id)

    async def renew_leases(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> int:
        now = _now()
        lease_expires_at = (
            datetime.now(UTC) + timedelta(seconds=max(0.1, lease_seconds))
        ).isoformat()

        def op() -> int:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                cursor = conn.execute(
                    "UPDATE agent_tasks SET lease_expires_at=?, updated_at=? "
                    "WHERE status='running' AND worker_id=?",
                    (lease_expires_at, now, worker_id),
                )
                conn.commit()
                return int(cursor.rowcount)

        return await _run_in_thread(op)

    async def reclaim_stale(
        self,
        *,
        worker_id: str = "lease-reaper",
        limit: int = 100,
    ) -> list[AgentTask]:
        now = _now()

        def op() -> list[Any]:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status='waiting_retry', worker_id=?, "
                    "next_retry_at=?, updated_at=? WHERE status='running' AND "
                    "(lease_expires_at IS NULL OR lease_expires_at <= ?)",
                    (worker_id, now, now, now),
                )
                conn.commit()
                return conn.execute(
                    f"SELECT {self._COLUMNS} FROM agent_tasks "
                    "WHERE status='waiting_retry' AND worker_id=? ORDER BY updated_at LIMIT ?",
                    (worker_id, limit),
                ).fetchall()

        rows = await _run_in_thread(op)
        return [_task_from_row(row) for row in rows]

    async def complete(
        self,
        task_id: str,
        *,
        worker_id: str,
        result: dict[str, Any] | None,
    ) -> AgentTask | None:
        now = _now()

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status='completed', result_json=?, error=NULL, "
                    "next_retry_at=NULL, updated_at=? WHERE task_id=? AND status='running' AND worker_id=?",
                    (
                        json.dumps(result, ensure_ascii=False) if result is not None else None,
                        now,
                        task_id,
                        worker_id,
                    ),
                )
                conn.commit()

        await _run_in_thread(op)
        task = await self.get(task_id)
        return task if task is not None and task.status == "completed" else None

    async def fail(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
    ) -> AgentTask | None:
        task = await self.get(task_id)
        if task is None or task.status != "running" or task.worker_id != worker_id:
            return None
        task.retry_count += 1
        task.error = error
        task.updated_at = _now()
        if task.retry_count < task.max_retries:
            task.status = "waiting_retry"
            task.next_retry_at = (
                datetime.now(UTC) + timedelta(seconds=max(0.1, retry_delay_seconds))
            ).isoformat()
        else:
            task.status = "failed"
            task.next_retry_at = None

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status=?, retry_count=?, error=?, next_retry_at=?, updated_at=? "
                    "WHERE task_id=? AND status='running' AND worker_id=?",
                    (
                        task.status,
                        task.retry_count,
                        task.error,
                        task.next_retry_at,
                        task.updated_at,
                        task_id,
                        worker_id,
                    ),
                )
                conn.commit()

        await _run_in_thread(op)
        return await self.get(task_id)

    async def mark_dead(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
    ) -> AgentTask | None:
        now = _now()

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status='dead', worker_id=?, error=?, "
                    "next_retry_at=NULL, updated_at=? WHERE task_id=?",
                    (worker_id, error, now, task_id),
                )
                conn.commit()

        await _run_in_thread(op)
        return await self.get(task_id)

    async def mark_requeued(
        self,
        task_id: str,
        *,
        worker_id: str,
    ) -> AgentTask | None:
        now = _now()

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status='queued', worker_id=?, error=NULL, "
                    "next_retry_at=NULL, updated_at=? WHERE task_id=? AND status='dead'",
                    (worker_id, now, task_id),
                )
                conn.commit()

        await _run_in_thread(op)
        task = await self.get(task_id)
        if task is not None and task.status == "queued" and task.worker_id == worker_id:
            return task
        return None


    async def mark_discarded(
        self,
        task_id: str,
        *,
        worker_id: str,
        reason: str,
    ) -> AgentTask | None:
        now = _now()

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status='discarded', worker_id=?, error=?, "
                    "next_retry_at=NULL, updated_at=? WHERE task_id=? AND status='dead'",
                    (worker_id, reason, now, task_id),
                )
                conn.commit()

        await _run_in_thread(op)
        task = await self.get(task_id)
        if task is not None and task.status == "discarded":
            return task
        return None


    async def set_waiting_retry(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
    ) -> AgentTask | None:
        now = _now()
        next_retry_at = (
            datetime.now(UTC) + timedelta(seconds=max(0.1, retry_delay_seconds))
        ).isoformat()

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status='waiting_retry', worker_id=?, error=?, "
                    "next_retry_at=?, updated_at=? WHERE task_id=?",
                    (worker_id, error, next_retry_at, now, task_id),
                )
                conn.commit()

        await _run_in_thread(op)
        task = await self.get(task_id)
        if task is not None and task.status == "waiting_retry":
            return task
        return None

    async def reschedule(self, task_id: str, *, worker_id: str = "scheduler") -> AgentTask | None:
        now = _now()

        def op() -> None:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status='queued', worker_id=?, next_retry_at=NULL, updated_at=? "
                    "WHERE task_id=? AND status='waiting_retry'",
                    (worker_id, now, task_id),
                )
                conn.commit()

        await _run_in_thread(op)
        task = await self.get(task_id)
        if task is not None and task.status == "queued" and task.worker_id == worker_id:
            return task
        return None

    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
    ) -> list[AgentTask]:
        def op() -> list[Any]:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                return conn.execute(
                    f"SELECT {self._COLUMNS} FROM agent_tasks WHERE status = ? "
                    "ORDER BY created_at LIMIT ?",
                    (status, limit),
                ).fetchall()

        rows = await _run_in_thread(op)
        return [_task_from_row(row) for row in rows]



    async def list_tasks(
        self,
        *,
        status: str | None = None,
        intent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        worker_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("intent", intent),
            ("tenant_id", tenant_id),
            ("user_id", user_id),
            ("worker_id", worker_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        def op() -> list[Any]:
            with sqlite3.connect(str(self._db_path), timeout=30.0) as conn:
                return conn.execute(
                    f"SELECT {self._COLUMNS} FROM agent_tasks{where} "
                    "ORDER BY created_at LIMIT ?",
                    (*values, max(1, int(limit))),
                ).fetchall()

        rows = await _run_in_thread(op)
        return [_task_from_row(row) for row in rows]

class PostgresAgentTaskStore(AgentTaskStore):
    DDL = """
    CREATE TABLE IF NOT EXISTS agent_tasks (
        task_id TEXT PRIMARY KEY,
        intent TEXT NOT NULL,
        status TEXT NOT NULL,
        queue TEXT NOT NULL,
        thread_id TEXT,
        user_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        idempotency_key TEXT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        result JSONB,
        error TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        worker_id TEXT,
        next_retry_at TIMESTAMPTZ,
        workflow_execution_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        lease_expires_at TIMESTAMPTZ,
        attempt_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_retry
        ON agent_tasks(status, next_retry_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_idempotency
        ON agent_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
    """

    _COLUMNS = (
        "task_id, intent, status, queue, thread_id, user_id, tenant_id, idempotency_key, "
        "payload, result, error, retry_count, max_retries, worker_id, "
        "next_retry_at, workflow_execution_id, created_at, updated_at, lease_expires_at, attempt_id"
    )

    @staticmethod
    def _values(task: AgentTask) -> tuple[Any, ...]:
        return (
            task.task_id,
            task.intent,
            task.status,
            task.queue,
            task.thread_id,
            task.user_id,
            task.tenant_id,
            task.idempotency_key,
            task.payload,
            task.result,
            task.error,
            task.retry_count,
            task.max_retries,
            task.worker_id,
            task.next_retry_at,
            task.workflow_execution_id,
            task.created_at,
            task.updated_at,
            task.lease_expires_at,
            task.attempt_id,
        )

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any | None = None

    async def initialize(self) -> None:
        import asyncpg  # type: ignore[import-untyped]

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        await self._pool.execute(self.DDL)
        await self._pool.execute(
            "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ"
        )
        await self._pool.execute(
            "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS attempt_id TEXT"
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresAgentTaskStore 未初始化，请先调用 initialize()")

    async def create(self, task: AgentTask) -> AgentTask:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            INSERT INTO agent_tasks ({self._COLUMNS})
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
            RETURNING {self._COLUMNS}
            """,
            *self._values(task),
        )
        return _task_from_row(row) if row is not None else task

    async def get(self, task_id: str) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"SELECT {self._COLUMNS} FROM agent_tasks WHERE task_id = $1",
            task_id,
        )
        return _task_from_row(row) if row is not None else None

    async def get_by_idempotency_key(self, key: str) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"SELECT {self._COLUMNS} FROM agent_tasks WHERE idempotency_key = $1",
            key,
        )
        return _task_from_row(row) if row is not None else None

    async def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
        attempt_id: str | None = None,
    ) -> AgentTask | None:
        self._check()
        current = await self.get(task_id)
        if current is not None and attempt_id is not None and current.attempt_id != attempt_id:
            return None
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET status='running', worker_id=$2, error=NULL, updated_at=now(),
                lease_expires_at=now() + make_interval(secs => $3::double precision)
            WHERE task_id=$1
              AND ($4::text IS NULL OR attempt_id=$4::text)
              AND (
                status IN ('queued','waiting_retry') OR (
                    status='running' AND (
                        lease_expires_at IS NULL OR lease_expires_at <= now()
                    )
                )
            )
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            max(0.1, lease_seconds),
            attempt_id,
        )
        return _task_from_row(row) if row is not None else None

    async def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET lease_expires_at=now() + make_interval(secs => $3::double precision),
                updated_at=now()
            WHERE task_id=$1 AND status='running' AND worker_id=$2
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            max(0.1, lease_seconds),
        )
        return _task_from_row(row) if row is not None else None

    async def renew_leases(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> int:
        self._check()
        assert self._pool is not None
        value = await self._pool.fetchval(
            """
            WITH updated AS (
                UPDATE agent_tasks
                SET lease_expires_at=now() + make_interval(secs => $2::double precision),
                    updated_at=now()
                WHERE status='running' AND worker_id=$1
                RETURNING 1
            )
            SELECT count(*) FROM updated
            """,
            worker_id,
            max(0.1, lease_seconds),
        )
        return int(value or 0)

    async def reclaim_stale(
        self,
        *,
        worker_id: str = "lease-reaper",
        limit: int = 100,
    ) -> list[AgentTask]:
        self._check()
        assert self._pool is not None
        rows = await self._pool.fetch(
            f"""
            WITH stale AS (
                UPDATE agent_tasks
                SET status='waiting_retry', worker_id=$1, next_retry_at=now(), updated_at=now()
                WHERE status='running' AND (
                    lease_expires_at IS NULL OR lease_expires_at <= now()
                )
                RETURNING {self._COLUMNS}
            )
            SELECT * FROM stale ORDER BY updated_at LIMIT $2
            """,
            worker_id,
            limit,
        )
        return [_task_from_row(row) for row in rows]

    async def complete(
        self,
        task_id: str,
        *,
        worker_id: str,
        result: dict[str, Any] | None,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET status='completed', result=$3, error=NULL, next_retry_at=NULL, updated_at=now()
            WHERE task_id=$1 AND status='running' AND worker_id=$2
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            result,
        )
        return _task_from_row(row) if row is not None else None

    async def fail(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET retry_count=retry_count+1,
                status=CASE WHEN retry_count+1 < max_retries THEN 'waiting_retry' ELSE 'failed' END,
                error=$3,
                next_retry_at=CASE WHEN retry_count+1 < max_retries
                    THEN now() + make_interval(secs => $4::double precision) ELSE NULL END,
                updated_at=now()
            WHERE task_id=$1 AND status='running' AND worker_id=$2
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            error,
            max(0.1, retry_delay_seconds),
        )
        return _task_from_row(row) if row is not None else None

    async def mark_dead(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET status='dead', worker_id=$2, error=$3, next_retry_at=NULL, updated_at=now()
            WHERE task_id=$1
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            error,
        )
        return _task_from_row(row) if row is not None else None

    async def mark_requeued(
        self,
        task_id: str,
        *,
        worker_id: str,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET status='queued', worker_id=$2, error=NULL, next_retry_at=NULL, updated_at=now()
            WHERE task_id=$1 AND status='dead'
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
        )
        return _task_from_row(row) if row is not None else None


    async def mark_discarded(
        self,
        task_id: str,
        *,
        worker_id: str,
        reason: str,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET status='discarded', worker_id=$2, error=$3, next_retry_at=NULL, updated_at=now()
            WHERE task_id=$1 AND status='dead'
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            reason,
        )
        return _task_from_row(row) if row is not None else None


    async def set_attempt(
        self,
        task_id: str,
        attempt_id: str,
        *,
        retry_count: int | None = None,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET attempt_id=$2,
                retry_count=COALESCE($3, retry_count),
                updated_at=now()
            WHERE task_id=$1 AND status IN ('queued','waiting_retry')
            RETURNING {self._COLUMNS}
            """,
            task_id,
            attempt_id,
            retry_count,
        )
        return _task_from_row(row) if row is not None else None

    async def set_waiting_retry(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET status='waiting_retry', worker_id=$2, error=$3,
                next_retry_at=now() + make_interval(secs => $4::double precision),
                updated_at=now()
            WHERE task_id=$1
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            error,
            max(0.1, retry_delay_seconds),
        )
        return _task_from_row(row) if row is not None else None

    async def reschedule(self, task_id: str, *, worker_id: str = "scheduler") -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET status='queued', worker_id=$2, next_retry_at=NULL, updated_at=now()
            WHERE task_id=$1 AND status='waiting_retry'
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
        )
        return _task_from_row(row) if row is not None else None

    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
    ) -> list[AgentTask]:
        self._check()
        assert self._pool is not None
        rows = await self._pool.fetch(
            f"SELECT {self._COLUMNS} FROM agent_tasks WHERE status = $1 "
            "ORDER BY created_at LIMIT $2",
            status,
            limit,
        )
        return [_task_from_row(row) for row in rows]



    async def list_tasks(
        self,
        *,
        status: str | None = None,
        intent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        worker_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        self._check()
        assert self._pool is not None
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("intent", intent),
            ("tenant_id", tenant_id),
            ("user_id", user_id),
            ("worker_id", worker_id),
        ):
            if value is not None:
                values.append(value)
                clauses.append(f"{column} = ${len(values)}")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, int(limit)))
        rows = await self._pool.fetch(
            f"SELECT {self._COLUMNS} FROM agent_tasks{where} "
            f"ORDER BY created_at LIMIT ${len(values)}",
            *values,
        )
        return [_task_from_row(row) for row in rows]

def create_agent_task_store(
    env: str,
    *,
    sqlite_path: str | Path,
    postgres_url: str | None = None,
) -> AgentTaskStore:
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryAgentTaskStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境 Agent 任务存储需要 PostgreSQL DSN")
        return PostgresAgentTaskStore(postgres_url)
    return SqliteAgentTaskStore(sqlite_path)


__all__ = [
    "AgentTask",
    "AgentTaskStore",
    "MemoryAgentTaskStore",
    "PostgresAgentTaskStore",
    "SqliteAgentTaskStore",
    "create_agent_task_store",
]


