"""SQLite 任务存储实现：开发环境持久化（单实例）。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_core.storage._sqlite_utils import _run_in_thread
from agent_core.storage._sqlite_utils import ensure_wal as _ensure_wal
from agent_core.tasks.base import AgentTaskStore
from agent_core.tasks.models import AgentTask, _now, _parse_time, _task_from_row

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


__all__ = ["SqliteAgentTaskStore"]
