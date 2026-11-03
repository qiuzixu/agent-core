"""基于 SQLite 的 RuntimeStore，用于开发与单实例部署。"""

from __future__ import annotations

import json
import sqlite3
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


class SqliteRuntimeStore(RuntimeStore):
    """开发环境 SQLite 实现，可和 sessions.db 共用数据库文件。"""

    DDL = """
    CREATE TABLE IF NOT EXISTS agent_runs (
        thread_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        user_id TEXT,
        tenant_id TEXT,
        parent_run_id TEXT,
        tags TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0,
        idempotency_key TEXT,
        iteration INTEGER NOT NULL DEFAULT 0,
        tool_calls_used INTEGER NOT NULL DEFAULT 0,
        pending_approval TEXT,
        pending_clarification TEXT,
        state TEXT NOT NULL DEFAULT '{}',
        metadata TEXT NOT NULL DEFAULT '{}',
        checkpoint TEXT NOT NULL DEFAULT '{}',
        events TEXT NOT NULL DEFAULT '[]',
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (thread_id, run_id)
    );
    CREATE TABLE IF NOT EXISTS agent_approvals (
        approval_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        user_id TEXT,
        tenant_id TEXT,
        action TEXT NOT NULL,
        arguments TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        decided_by TEXT,
        decided_at TEXT
    );
    CREATE TABLE IF NOT EXISTS agent_threads (
        thread_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """

    def __init__(self, db_path: str | Path = "./sessions.db") -> None:
        self._db_path = str(Path(db_path).resolve())
        # 建表/补列/幂等索引 DDL 初始化同样放入工作线程执行，与 async 方法共用同一连接参数规范。
        run_sync_in_thread(self._init_db)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(self.DDL)
            self._ensure_columns(conn)

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        """为已有开发数据库补充新字段，避免升级时丢失运行记录。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")}
        for name, definition in {
            "tenant_id": "TEXT",
            "parent_run_id": "TEXT",
            "tags": "TEXT NOT NULL DEFAULT '[]'",
            "version": "INTEGER NOT NULL DEFAULT 0",
            "idempotency_key": "TEXT",
            "checkpoint": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {name} {definition}")
        approval_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_approvals)")}
        for name, definition in {
            "user_id": "TEXT",
            "tenant_id": "TEXT",
            "decided_by": "TEXT",
            "decided_at": "TEXT",
        }.items():
            if name not in approval_columns:
                conn.execute(f"ALTER TABLE agent_approvals ADD COLUMN {name} {definition}")
        # SQLite 把 NULL 视为互不相等，表达式索引把匿名作用域归一化后才能真正幂等。
        conn.execute("DROP INDEX IF EXISTS idx_agent_runs_idempotency")
        duplicate = conn.execute(
            "SELECT COALESCE(tenant_id, ''), COALESCE(user_id, ''), idempotency_key "
            "FROM agent_runs WHERE idempotency_key IS NOT NULL "
            "GROUP BY 1, 2, 3 HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate:
            # 旧索引允许 (NULL, NULL, key) 重复；直接建唯一索引会让升级中断且难以定位。
            raise RuntimeConcurrencyError(
                f"agent_runs 存在归一化后重复的幂等键 {tuple(duplicate)}，"
                "无法创建唯一索引；请先清理历史重复行后再初始化"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_idempotency_scope "
            "ON agent_runs(COALESCE(tenant_id, ''), COALESCE(user_id, ''), idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """获取 SQLite 连接（同步，自动提交/回滚；统一 timeout 与 WAL 规范）。"""
        with sqlite_connection(self._db_path) as conn:
            yield conn

    async def save_run(self, context: RunContext) -> None:
        def _op() -> int:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT version FROM agent_runs WHERE thread_id=? AND run_id=?",
                    (context.thread_id, context.run_id),
                ).fetchone()
                stored_version = int(row[0]) if row else 0
                if row is not None and stored_version != context.version:
                    raise RuntimeConcurrencyError(f"run {context.run_id} 版本冲突")
                next_version = stored_version + 1
                value = context.to_dict()
                try:
                    cursor = conn.execute(
                        """INSERT INTO agent_runs
                        (thread_id, run_id, user_id, tenant_id, parent_run_id, tags,
                         status, version, idempotency_key,
                         iteration, tool_calls_used, pending_approval, pending_clarification,
                         state, metadata, checkpoint, events)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(thread_id, run_id) DO UPDATE SET
                          user_id=excluded.user_id, tenant_id=excluded.tenant_id,
                          parent_run_id=excluded.parent_run_id, tags=excluded.tags,
                          status=excluded.status, version=excluded.version,
                          idempotency_key=excluded.idempotency_key,
                          iteration=excluded.iteration, tool_calls_used=excluded.tool_calls_used,
                          pending_approval=excluded.pending_approval,
                          pending_clarification=excluded.pending_clarification,
                          state=excluded.state, metadata=excluded.metadata,
                          checkpoint=excluded.checkpoint,
                          events=excluded.events, updated_at=CURRENT_TIMESTAMP
                        WHERE agent_runs.version=?""",
                        (
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
                            json.dumps(value["pending_approval"], ensure_ascii=False),
                            json.dumps(value["pending_clarification"], ensure_ascii=False),
                            json.dumps(value["state"], ensure_ascii=False),
                            json.dumps(value["metadata"], ensure_ascii=False),
                            json.dumps(value["checkpoint"], ensure_ascii=False),
                            json.dumps(value["events"], ensure_ascii=False),
                            stored_version,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    if context.idempotency_key and "idempotency" in str(exc).lower():
                        raise RuntimeConcurrencyError(
                            f"幂等键已被其他 run 使用：{context.idempotency_key}"
                        ) from exc
                    raise
                if cursor.rowcount != 1:
                    raise RuntimeConcurrencyError(f"run {context.run_id} 保存时发生并发冲突")
            return next_version

        next_version = await _run_in_thread(_op)
        context.version = next_version
        if context.pending_approval:
            await self.save_approval(context.pending_approval)

    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None:
        def _op() -> Any:
            with self._connect() as conn:
                return conn.execute(
                    """SELECT thread_id, run_id, user_id, tenant_id, parent_run_id, tags,
                       status, version,
                       idempotency_key, iteration, tool_calls_used, pending_approval,
                       pending_clarification, state, metadata, checkpoint, events
                       FROM agent_runs WHERE thread_id = ? AND run_id = ?""",
                    (thread_id, run_id),
                ).fetchone()

        row = await _run_in_thread(_op)
        if row is None:
            return None
        # 使用固定列名，避免依赖 sqlite Row 配置。
        keys = [
            "thread_id",
            "run_id",
            "user_id",
            "tenant_id",
            "parent_run_id",
            "tags",
            "status",
            "version",
            "idempotency_key",
            "iteration",
            "tool_calls_used",
            "pending_approval",
            "pending_clarification",
            "state",
            "metadata",
            "checkpoint",
            "events",
        ]
        value = dict(zip(keys, row, strict=True))
        for key in (
            "tags",
            "pending_approval",
            "pending_clarification",
            "state",
            "metadata",
            "checkpoint",
            "events",
        ):
            value[key] = json.loads(value[key]) if value[key] else (None if key.startswith("pending") else {})
        return _context_from_dict(value)

    async def list_runs(self, thread_id: str) -> list[RunContext]:
        def _op() -> list[Any]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT run_id FROM agent_runs WHERE thread_id = ? ORDER BY updated_at DESC",
                    (thread_id,),
                ).fetchall()

        rows = await _run_in_thread(_op)
        result: list[RunContext] = []
        for (run_id,) in rows:
            context = await self.load_run(thread_id, run_id)
            if context:
                result.append(context)
        return result

    async def list_runs_by_status(self, status: str) -> list[RunContext]:
        def _op() -> list[Any]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT thread_id, run_id FROM agent_runs WHERE status = ? ORDER BY updated_at DESC",
                    (status,),
                ).fetchall()

        rows = await _run_in_thread(_op)
        result: list[RunContext] = []
        for thread_id, run_id in rows:
            context = await self.load_run(thread_id, run_id)
            if context:
                result.append(context)
        return result

    async def find_run_by_idempotency(
        self, tenant_id: str | None, user_id: str | None, idempotency_key: str
    ) -> RunContext | None:
        def _op() -> Any:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT thread_id, run_id FROM agent_runs "
                    "WHERE tenant_id IS ? AND user_id IS ? AND idempotency_key = ? LIMIT 1",
                    (tenant_id, user_id, idempotency_key),
                ).fetchone()

        row = await _run_in_thread(_op)
        return await self.load_run(row[0], row[1]) if row else None

    async def mark_stale_runs(self) -> list[RunContext]:
        def _op() -> list[Any]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT thread_id, run_id FROM agent_runs WHERE status IN ('queued', 'running')"
                ).fetchall()

        rows = await _run_in_thread(_op)
        result: list[RunContext] = []
        for thread_id, run_id in rows:
            context = await self.load_run(thread_id, run_id)
            if context is None:
                continue
            transition_run_status(context, "interrupted")
            context.emit("run_interrupted", reason="服务重启")
            await self.save_run(context)
            result.append(context)
        return result

    async def save_thread_owner(self, thread_id: str, user_id: str, tenant_id: str) -> None:
        def _op() -> None:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT user_id, tenant_id FROM agent_threads WHERE thread_id=?", (thread_id,)
                ).fetchone()
                if existing and tuple(existing) != (user_id, tenant_id):
                    raise RuntimeConcurrencyError("thread 归属冲突")
                conn.execute(
                    "INSERT OR IGNORE INTO agent_threads(thread_id,user_id,tenant_id) VALUES (?,?,?)",
                    (thread_id, user_id, tenant_id),
                )

        await _run_in_thread(_op)

    async def get_thread_owner(self, thread_id: str) -> tuple[str, str] | None:
        def _op() -> Any:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT user_id, tenant_id FROM agent_threads WHERE thread_id=?", (thread_id,)
                ).fetchone()

        row = await _run_in_thread(_op)
        return tuple(row) if row else None

    async def save_approval(self, approval: ApprovalRecord) -> None:
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO agent_approvals
                    (approval_id, thread_id, run_id, user_id, tenant_id, action,
                     arguments, status, reason, created_at, expires_at, decided_by, decided_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(approval_id) DO UPDATE SET
                        status=excluded.status, reason=excluded.reason,
                        expires_at=excluded.expires_at,
                        decided_by=excluded.decided_by, decided_at=excluded.decided_at
                    WHERE agent_approvals.status='pending'""",
                    (
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
                    ),
                )

        await _run_in_thread(_op)

    async def transition_approval(
        self,
        approval: ApprovalRecord,
        *,
        expected_status: ApprovalStatus = "pending",
    ) -> bool:
        def _op() -> int:
            with self._connect() as conn:
                cursor = conn.execute(
                    """UPDATE agent_approvals
                    SET status=?, reason=?, expires_at=?, decided_by=?, decided_at=?
                    WHERE approval_id=? AND status=?""",
                    (
                        approval.status,
                        approval.reason,
                        approval.expires_at,
                        approval.decided_by,
                        approval.decided_at,
                        approval.approval_id,
                        expected_status,
                    ),
                )
            return cursor.rowcount

        return await _run_in_thread(_op) == 1

    @staticmethod
    def _approval_row_to_record(row: Any) -> ApprovalRecord:
        """把 SQLite 查询行转换为审批记录。"""
        record = _approval_from_dict(
            dict(
                zip(
                    [
                        "approval_id",
                        "thread_id",
                        "run_id",
                        "user_id",
                        "tenant_id",
                        "action",
                        "arguments",
                        "status",
                        "reason",
                        "created_at",
                        "expires_at",
                        "decided_by",
                        "decided_at",
                    ],
                    row,
                    strict=True,
                )
            )
        )
        # 查询行必含主键，转换结果不可能为 None。
        assert record is not None
        return record

    async def load_approval(self, approval_id: str) -> ApprovalRecord | None:
        def _op() -> Any:
            with self._connect() as conn:
                return conn.execute(
                    """SELECT approval_id, thread_id, run_id, user_id, tenant_id,
                    action, arguments, status, reason, created_at, expires_at,
                    decided_by, decided_at
                    FROM agent_approvals WHERE approval_id = ?""",
                    (approval_id,),
                ).fetchone()

        row = await _run_in_thread(_op)
        if row is None:
            return None
        return self._approval_row_to_record(row)

    async def list_approvals(
        self, *, status: str = "pending", limit: int = 100
    ) -> list[ApprovalRecord]:
        """按状态列出审批记录（跨进程可见的待审批列表来源）。"""
        def _op() -> list[ApprovalRecord]:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT approval_id, thread_id, run_id, user_id, tenant_id,
                    action, arguments, status, reason, created_at, expires_at,
                    decided_by, decided_at
                    FROM agent_approvals WHERE status = ?
                    ORDER BY created_at DESC LIMIT ?""",
                    (status, limit),
                ).fetchall()
            return [self._approval_row_to_record(row) for row in rows]

        return await _run_in_thread(_op)

    async def purge_approvals(self, before: str) -> int:
        """清理早于 before 的终态审批记录；pending 永不清理。"""
        def _op() -> int:
            with self._connect() as conn:
                cursor = conn.execute(
                    """DELETE FROM agent_approvals
                    WHERE status IN ('approved', 'rejected', 'expired')
                    AND COALESCE(decided_at, created_at) <= ?""",
                    (before,),
                )
            return cursor.rowcount

        return await _run_in_thread(_op)


# ──────────────────────────────────────────────
# 4. PostgreSQL 后端（生产环境）
# ──────────────────────────────────────────────
