"""PostgreSQL 任务存储实现：生产环境多实例部署。"""

from __future__ import annotations

from typing import Any

from agent_core.tasks.base import AgentTaskStore
from agent_core.tasks.models import AgentTask, _task_from_row

class PostgresAgentTaskStore(AgentTaskStore):
    SCHEMA_VERSION = 2
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
        attempt_id TEXT,
        fencing_token BIGINT NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_retry
        ON agent_tasks(status, next_retry_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_idempotency
        ON agent_tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
    """

    _COLUMNS = (
        "task_id, intent, status, queue, thread_id, user_id, tenant_id, idempotency_key, "
        "payload, result, error, retry_count, max_retries, worker_id, "
        "next_retry_at, workflow_execution_id, created_at, updated_at, lease_expires_at, attempt_id, fencing_token"
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
            task.fencing_token,
        )

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any | None = None

    async def initialize(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 多实例同时启动时只允许一个进程执行 DDL/迁移，避免索引和列变更竞态。
                await conn.fetchval(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    "agent_core.agent_tasks.schema",
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_core_schema_versions (
                        component TEXT PRIMARY KEY,
                        version INTEGER NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                await conn.execute(self.DDL)
                version = await conn.fetchval(
                    "SELECT version FROM agent_core_schema_versions WHERE component=$1",
                    "agent_tasks",
                )
                current_version = int(version or 0)
                if current_version < 1:
                    await conn.execute(
                        "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ"
                    )
                if current_version < 2:
                    await conn.execute("ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS attempt_id TEXT")
                    await conn.execute(
                        "ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS fencing_token BIGINT NOT NULL DEFAULT 0"
                    )
                await conn.execute(
                    """
                    INSERT INTO agent_core_schema_versions(component, version)
                    VALUES ($1, $2)
                    ON CONFLICT(component) DO UPDATE SET version=EXCLUDED.version, updated_at=now()
                    """,
                    "agent_tasks",
                    self.SCHEMA_VERSION,
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
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)
            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
            DO UPDATE SET task_id=agent_tasks.task_id
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
                lease_expires_at=now() + make_interval(secs => $3::double precision),
                fencing_token=agent_tasks.fencing_token + 1
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
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET lease_expires_at=now() + make_interval(secs => $3::double precision),
                updated_at=now()
            WHERE task_id=$1 AND status='running' AND worker_id=$2
              AND ($4::bigint IS NULL OR fencing_token=$4::bigint)
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            max(0.1, lease_seconds),
            fencing_token,
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
            WITH selected AS (
                SELECT task_id FROM agent_tasks
                WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                ORDER BY updated_at
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            ), stale AS (
                UPDATE agent_tasks
                SET status='waiting_retry', worker_id=$1, next_retry_at=now(), updated_at=now()
                WHERE task_id IN (SELECT task_id FROM selected)
                RETURNING {self._COLUMNS}
            )
            SELECT * FROM stale ORDER BY updated_at
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
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET status='completed', result=$3, error=NULL, next_retry_at=NULL, updated_at=now()
            WHERE task_id=$1 AND status='running' AND worker_id=$2
              AND ($4::bigint IS NULL OR fencing_token=$4::bigint)
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            result,
            fencing_token,
        )
        return _task_from_row(row) if row is not None else None

    async def fail(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
        fencing_token: int | None = None,
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
              AND ($5::bigint IS NULL OR fencing_token=$5::bigint)
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            error,
            max(0.1, retry_delay_seconds),
            fencing_token,
        )
        return _task_from_row(row) if row is not None else None

    async def mark_dead(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        self._check()
        assert self._pool is not None
        row = await self._pool.fetchrow(
            f"""
            UPDATE agent_tasks
            SET status='dead', worker_id=$2, error=$3, next_retry_at=NULL, updated_at=now()
            WHERE task_id=$1 AND status='running' AND worker_id=$2
              AND ($4::bigint IS NULL OR fencing_token=$4::bigint)
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            error,
            fencing_token,
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
        fencing_token: int | None = None,
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
              AND ($5::bigint IS NULL OR fencing_token=$5::bigint)
            RETURNING {self._COLUMNS}
            """,
            task_id,
            worker_id,
            error,
            max(0.1, retry_delay_seconds),
            fencing_token,
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


__all__ = ["PostgresAgentTaskStore"]
