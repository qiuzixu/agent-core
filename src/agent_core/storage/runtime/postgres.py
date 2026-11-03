"""基于 PostgreSQL 的 RuntimeStore，用于生产与多实例部署。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_core.protocol.runtime import (
    ApprovalRecord,
    ApprovalStatus,
    RunContext,
)
from agent_core.runtime.state import transition_run_status
from agent_core.storage._sqlite_utils import (
    _run_in_thread,
    run_sync_in_thread,
    sqlite_connection,
)
from agent_core.storage.runtime.base import (
    RuntimeConcurrencyError,
    RuntimeStore,
    _approval_from_dict,
    _context_from_dict,
)


class PostgresRuntimeStore(RuntimeStore):
    """生产环境 PostgreSQL 实现。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any = None

    async def initialize(self) -> None:
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError("PostgresRuntimeStore 需要 asyncpg，请执行：uv add asyncpg") from exc
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    thread_id TEXT NOT NULL, run_id TEXT NOT NULL, user_id TEXT, tenant_id TEXT,
                    parent_run_id TEXT, tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                    status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT,
                    iteration INTEGER NOT NULL DEFAULT 0,
                    tool_calls_used INTEGER NOT NULL DEFAULT 0,
                    state JSONB NOT NULL DEFAULT '{}'::jsonb,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb,
                    pending_approval JSONB, pending_clarification JSONB,
                    events JSONB NOT NULL DEFAULT '[]'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (thread_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS agent_approvals (
                    approval_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    user_id TEXT, tenant_id TEXT,
                    action TEXT NOT NULL, arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
                    status TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, expires_at TEXT,
                    decided_by TEXT, decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS agent_threads (
                    thread_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS tenant_id TEXT;
                ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS parent_run_id TEXT;
                ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb;
                ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
                ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS checkpoint JSONB NOT NULL DEFAULT '{}'::jsonb;
                ALTER TABLE agent_approvals ADD COLUMN IF NOT EXISTS user_id TEXT;
                ALTER TABLE agent_approvals ADD COLUMN IF NOT EXISTS tenant_id TEXT;
                ALTER TABLE agent_approvals ADD COLUMN IF NOT EXISTS decided_by TEXT;
                ALTER TABLE agent_approvals ADD COLUMN IF NOT EXISTS decided_at TEXT;
                DROP INDEX IF EXISTS idx_agent_runs_idempotency;
            """)
            # 旧索引允许 (NULL, NULL, key) 重复；先预检归一化后的重复行再建唯一索引。
            duplicate = await conn.fetchrow(
                "SELECT COALESCE(tenant_id, ''), COALESCE(user_id, ''), idempotency_key "
                "FROM agent_runs WHERE idempotency_key IS NOT NULL "
                "GROUP BY 1, 2, 3 HAVING COUNT(*) > 1 LIMIT 1"
            )
            if duplicate:
                raise RuntimeConcurrencyError(
                    f"agent_runs 存在归一化后重复的幂等键 {tuple(duplicate)}，"
                    "无法创建唯一索引；请先清理历史重复行后再初始化"
                )
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_idempotency_scope "
                "ON agent_runs(COALESCE(tenant_id, ''), COALESCE(user_id, ''), idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresRuntimeStore 未初始化")

    async def save_run(self, context: RunContext) -> None:
        self._check()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 在同一事务中锁住当前 run，避免两个 API 实例同时通过版本检查。
                row = await conn.fetchrow(
                    "SELECT version, thread_id, run_id FROM agent_runs "
                    "WHERE thread_id=$1 AND run_id=$2 FOR UPDATE",
                    context.thread_id,
                    context.run_id,
                )
                stored_version = int(row["version"]) if row is not None else None
                if stored_version is not None and stored_version != context.version:
                    raise RuntimeConcurrencyError(f"run {context.run_id} 版本冲突")

                # 幂等键的唯一索引负责最终约束，这里先给出可理解的领域错误，
                # 避免把 asyncpg 的 UniqueViolation 泄漏到 HTTP 层。
                if context.idempotency_key:
                    duplicate = await conn.fetchrow(
                        "SELECT thread_id, run_id FROM agent_runs "
                        "WHERE tenant_id IS NOT DISTINCT FROM $1 "
                        "AND user_id IS NOT DISTINCT FROM $2 "
                        "AND idempotency_key=$3 FOR SHARE",
                        context.tenant_id,
                        context.user_id,
                        context.idempotency_key,
                    )
                    if duplicate and (
                        duplicate["thread_id"] != context.thread_id or duplicate["run_id"] != context.run_id
                    ):
                        raise RuntimeConcurrencyError(f"幂等键已被 run {duplicate['run_id']} 使用")

                next_version = (stored_version or 0) + 1
                value = context.to_dict()
                command = await conn.execute(
                    """INSERT INTO agent_runs
                    (thread_id, run_id, user_id, tenant_id, parent_run_id, tags,
                     status, version, idempotency_key,
                     iteration, tool_calls_used, state, metadata, checkpoint,
                     pending_approval, pending_clarification, events)
                    VALUES (
                      $1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,
                      $14::jsonb,$15::jsonb,$16::jsonb,$17::jsonb
                    )
                    ON CONFLICT (thread_id, run_id) DO UPDATE SET
                      user_id=$3, tenant_id=$4, parent_run_id=$5, tags=$6::jsonb,
                      status=$7, version=$8, idempotency_key=$9,
                      iteration=$10, tool_calls_used=$11,
                      state=$12::jsonb, metadata=$13::jsonb, checkpoint=$14::jsonb,
                      pending_approval=$15::jsonb, pending_clarification=$16::jsonb,
                      events=$17::jsonb, updated_at=NOW()
                    WHERE agent_runs.version=$18""",
                    context.thread_id,
                    context.run_id,
                    context.user_id,
                    context.tenant_id,
                    context.parent_run_id,
                    json.dumps(value["tags"], ensure_ascii=False),
                    context.status,
                    next_version,
                    context.idempotency_key,
                    context.iteration,
                    context.tool_calls_used,
                    json.dumps(value["state"], ensure_ascii=False),
                    json.dumps(value["metadata"], ensure_ascii=False),
                    json.dumps(value["checkpoint"], ensure_ascii=False),
                    json.dumps(value["pending_approval"], ensure_ascii=False),
                    json.dumps(value["pending_clarification"], ensure_ascii=False),
                    json.dumps(value["events"], ensure_ascii=False),
                    stored_version or 0,
                )
                expected_command = "INSERT 0 1" if stored_version is None else "UPDATE 1"
                if command != expected_command:
                    raise RuntimeConcurrencyError(f"run {context.run_id} 保存时发生并发冲突")
            context.version = next_version
        if context.pending_approval:
            await self.save_approval(context.pending_approval)

    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None:
        self._check()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_runs WHERE thread_id=$1 AND run_id=$2", thread_id, run_id
            )
        if row is None:
            return None
        return _context_from_dict(dict(row))

    async def list_runs(self, thread_id: str) -> list[RunContext]:
        self._check()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agent_runs WHERE thread_id=$1 ORDER BY updated_at DESC", thread_id
            )
        return [_context_from_dict(dict(row)) for row in rows]

    async def list_runs_by_status(self, status: str) -> list[RunContext]:
        self._check()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agent_runs WHERE status=$1 ORDER BY updated_at DESC", status
            )
        return [_context_from_dict(dict(row)) for row in rows]

    async def find_run_by_idempotency(
        self, tenant_id: str | None, user_id: str | None, idempotency_key: str
    ) -> RunContext | None:
        self._check()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT thread_id, run_id FROM agent_runs "
                "WHERE tenant_id IS NOT DISTINCT FROM $1 AND user_id IS NOT DISTINCT FROM $2 "
                "AND idempotency_key=$3 LIMIT 1",
                tenant_id,
                user_id,
                idempotency_key,
            )
        return await self.load_run(row["thread_id"], row["run_id"]) if row else None

    async def mark_stale_runs(self) -> list[RunContext]:
        self._check()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT thread_id, run_id FROM agent_runs WHERE status IN ('queued', 'running')"
            )
        result: list[RunContext] = []
        for row in rows:
            context = await self.load_run(row["thread_id"], row["run_id"])
            if context is None:
                continue
            transition_run_status(context, "interrupted")
            context.emit("run_interrupted", reason="服务重启")
            await self.save_run(context)
            result.append(context)
        return result

    async def save_thread_owner(self, thread_id: str, user_id: str, tenant_id: str) -> None:
        self._check()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_threads(thread_id,user_id,tenant_id) VALUES ($1,$2,$3)
                ON CONFLICT(thread_id) DO UPDATE SET user_id=agent_threads.user_id,
                tenant_id=agent_threads.tenant_id""",
                thread_id,
                user_id,
                tenant_id,
            )
            owner = await conn.fetchrow(
                "SELECT user_id, tenant_id FROM agent_threads WHERE thread_id=$1", thread_id
            )
            if owner["user_id"] != user_id or owner["tenant_id"] != tenant_id:
                raise RuntimeConcurrencyError("thread 归属冲突")

    async def get_thread_owner(self, thread_id: str) -> tuple[str, str] | None:
        self._check()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, tenant_id FROM agent_threads WHERE thread_id=$1", thread_id
            )
        return (row["user_id"], row["tenant_id"]) if row else None

    async def save_approval(self, approval: ApprovalRecord) -> None:
        self._check()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO agent_approvals (
                    approval_id,thread_id,run_id,user_id,tenant_id,action,
                    arguments,status,reason,created_at,expires_at,decided_by,decided_at
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (approval_id) DO UPDATE SET
                    status=$8, reason=$9, expires_at=$11, decided_by=$12, decided_at=$13
                WHERE agent_approvals.status='pending'""",
                approval.approval_id,
                approval.thread_id,
                approval.run_id,
                approval.user_id,
                approval.tenant_id,
                approval.action,
                json.dumps(approval.arguments, ensure_ascii=False),
                approval.status,
                approval.reason,
                approval.created_at,
                approval.expires_at,
                approval.decided_by,
                approval.decided_at,
            )

    async def transition_approval(
        self,
        approval: ApprovalRecord,
        *,
        expected_status: ApprovalStatus = "pending",
    ) -> bool:
        self._check()
        async with self._pool.acquire() as conn:
            command = await conn.execute(
                """UPDATE agent_approvals
                SET status=$2, reason=$3, expires_at=$4, decided_by=$5, decided_at=$6
                WHERE approval_id=$1 AND status=$7""",
                approval.approval_id,
                approval.status,
                approval.reason,
                approval.expires_at,
                approval.decided_by,
                approval.decided_at,
                expected_status,
            )
        return bool(command == "UPDATE 1")

    async def load_approval(self, approval_id: str) -> ApprovalRecord | None:
        self._check()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_approvals WHERE approval_id=$1", approval_id)
        return _approval_from_dict(dict(row)) if row else None

    async def list_approvals(
        self, *, status: str = "pending", limit: int = 100
    ) -> list[ApprovalRecord]:
        """按状态列出审批记录（跨进程可见的待审批列表来源）。"""
        self._check()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agent_approvals WHERE status=$1 ORDER BY created_at DESC LIMIT $2",
                status,
                limit,
            )
        return [record for record in (_approval_from_dict(dict(row)) for row in rows) if record]

    async def purge_approvals(self, before: str) -> int:
        """清理早于 before 的终态审批记录；pending 永不清理。"""
        self._check()
        async with self._pool.acquire() as conn:
            command = await conn.execute(
                """DELETE FROM agent_approvals
                WHERE status IN ('approved', 'rejected', 'expired')
                AND COALESCE(decided_at, created_at) <= $1""",
                before,
            )
        tag = command.split()[-1] if command else "0"
        return int(tag) if tag.isdigit() else 0


# ──────────────────────────────────────────────
# 5. 创建运行时状态存储
# ──────────────────────────────────────────────
