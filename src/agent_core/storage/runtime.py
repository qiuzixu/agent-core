"""运行时状态存储。

测试使用内存，开发使用 SQLite，生产使用 PostgreSQL。
RunContext 等领域对象不绑定具体数据库。

存 agent 引擎的执行状态
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_core.protocol.runtime import (
    ApprovalRecord,
    RunContext,
)


# ————————————————————————————————————#
# 运行时并发错误
# ————————————————————————————————————#
class RuntimeConcurrencyError(RuntimeError):
    """运行状态版本冲突，调用方应重新加载后再决定是否重试。"""


# ————————————————————————————————————#
# 运行时存储接口
# ————————————————————————————————————#
class RuntimeStore(ABC):
    """运行状态、事件和审批记录的持久化抽象。"""

    # ————————————————————————————————————#
    # 运行状态存储
    # ————————————————————————————————————#
    @abstractmethod
    async def save_run(self, context: RunContext) -> None: ...

    # ————————————————————————————————————#
    # 运行事件查询
    # ————————————————————————————————————#
    @abstractmethod
    async def find_run_by_idempotency(
        self, tenant_id: str | None, user_id: str | None, idempotency_key: str
    ) -> RunContext | None: ...

    # ————————————————————————————————————#
    # 运行状态查询
    # ————————————————————————————————————#
    @abstractmethod
    async def mark_stale_runs(self) -> list[RunContext]: ...

    # ————————————————————————————————————#
    # 运行状态查询
    # ————————————————————————————————————#
    @abstractmethod
    async def save_thread_owner(self, thread_id: str, user_id: str, tenant_id: str) -> None: ...

    # ————————————————————————————————————#
    # 运行状态查询
    # ————————————————————————————————————#
    @abstractmethod
    async def get_thread_owner(self, thread_id: str) -> tuple[str, str] | None: ...

    # ————————————————————————————————————#
    # 运行状态加载
    # ————————————————————————————————————#
    @abstractmethod
    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None: ...

    @abstractmethod
    async def list_runs(self, thread_id: str) -> list[RunContext]: ...

    @abstractmethod
    async def save_approval(self, approval: ApprovalRecord) -> None: ...

    @abstractmethod
    async def load_approval(self, approval_id: str) -> ApprovalRecord | None: ...

    @abstractmethod
    async def transition_approval(
        self,
        approval: ApprovalRecord,
        *,
        expected_status: str = "pending",
    ) -> bool: ...

    # ————————————————————————————————————#
    # 运行审批记录加载
    # ————————————————————————————————————#


def _approval_from_dict(value: dict[str, Any] | None) -> ApprovalRecord | None:
    if not value:
        return None
    arguments = value.get("arguments") or {}
    if isinstance(arguments, str):  # asyncpg 默认把 JSONB 返回为字符串，统一转换后再重建领域对象。
        arguments = json.loads(arguments) if arguments else {}  # 确保 arguments 是字典类型
    return ApprovalRecord.from_dict({**value, "arguments": dict(arguments)})


# ──────────────────────────────────────────────
# 数据库行 → RunContext
# ──────────────────────────────────────────────
def _context_from_dict(value: dict[str, Any]) -> RunContext:
    value = copy.deepcopy(value)
    # asyncpg 默认把 JSONB 返回为字符串，统一转换后再重建领域对象。
    for key in (
        "tags",
        "pending_approval",
        "pending_clarification",
        "state",
        "metadata",
        "checkpoint",
        "events",
    ):
        if isinstance(value.get(key), str):
            value[key] = json.loads(value[key])
    return RunContext.from_dict(value)


# ──────────────────────────────────────────────
# 2. 内存后端（测试环境）
# ──────────────────────────────────────────────
class MemoryRuntimeStore(RuntimeStore):
    """测试用内存实现。"""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], dict[str, Any]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def save_run(self, context: RunContext) -> None:
        key = (context.thread_id, context.run_id)
        previous = self._runs.get(key)
        if previous is not None and int(previous.get("version", 0)) != context.version:
            raise RuntimeConcurrencyError(f"run {context.run_id} 版本冲突")
        context.version = (int(previous.get("version", 0)) + 1) if previous else 1
        self._runs[key] = copy.deepcopy(context.to_dict())
        if context.pending_approval:
            await self.save_approval(context.pending_approval)

    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None:
        value = self._runs.get((thread_id, run_id))
        return _context_from_dict(value) if value else None

    async def list_runs(self, thread_id: str) -> list[RunContext]:
        return [
            _context_from_dict(value)
            for (stored_thread, _), value in self._runs.items()
            if stored_thread == thread_id
        ]

    async def find_run_by_idempotency(
        self, tenant_id: str | None, user_id: str | None, idempotency_key: str
    ) -> RunContext | None:
        for value in self._runs.values():
            if (
                value.get("tenant_id") == tenant_id
                and value.get("user_id") == user_id
                and value.get("idempotency_key") == idempotency_key
            ):
                return _context_from_dict(value)
        return None

    async def mark_stale_runs(self) -> list[RunContext]:
        stale: list[RunContext] = []
        for value in list(self._runs.values()):
            if value.get("status") not in {"queued", "running"}:
                continue
            context = _context_from_dict(value)
            context.status = "interrupted"
            context.emit("run_interrupted", reason="服务重启")
            context.version = int(value.get("version", 0))
            await self.save_run(context)
            stale.append(context)
        return stale

    async def save_thread_owner(self, thread_id: str, user_id: str, tenant_id: str) -> None:
        previous = self._threads.get(thread_id)
        if previous and previous != (user_id, tenant_id):
            raise RuntimeConcurrencyError("thread 归属冲突")
        self._threads[thread_id] = (user_id, tenant_id)

    async def get_thread_owner(self, thread_id: str) -> tuple[str, str] | None:
        return self._threads.get(thread_id)

    async def save_approval(self, approval: ApprovalRecord) -> None:
        async with self._lock:
            current = self._approvals.get(approval.approval_id)
            if current is not None and current.get("status") != "pending":
                return
            self._approvals[approval.approval_id] = copy.deepcopy(approval.to_dict())

    async def load_approval(self, approval_id: str) -> ApprovalRecord | None:
        return _approval_from_dict(copy.deepcopy(self._approvals.get(approval_id)))

    async def transition_approval(
        self,
        approval: ApprovalRecord,
        *,
        expected_status: str = "pending",
    ) -> bool:
        async with self._lock:
            current = self._approvals.get(approval.approval_id)
            if current is None or current.get("status") != expected_status:
                return False
            self._approvals[approval.approval_id] = copy.deepcopy(approval.to_dict())
            return True


# ──────────────────────────────────────────────
# 3. SQLite 后端（开发环境）
# ──────────────────────────────────────────────
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
        expires_at TEXT
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
        for name, definition in {"user_id": "TEXT", "tenant_id": "TEXT"}.items():
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
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def save_run(self, context: RunContext) -> None:
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
            if cursor.rowcount != 1:
                raise RuntimeConcurrencyError(f"run {context.run_id} 保存时发生并发冲突")
        context.version = next_version
        if context.pending_approval:
            await self.save_approval(context.pending_approval)

    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT thread_id, run_id, user_id, tenant_id, parent_run_id, tags,
                   status, version,
                   idempotency_key, iteration, tool_calls_used, pending_approval,
                   pending_clarification, state, metadata, checkpoint, events
                   FROM agent_runs WHERE thread_id = ? AND run_id = ?""",
                (thread_id, run_id),
            ).fetchone()
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
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id FROM agent_runs WHERE thread_id = ? ORDER BY updated_at DESC",
                (thread_id,),
            ).fetchall()
        result: list[RunContext] = []
        for (run_id,) in rows:
            context = await self.load_run(thread_id, run_id)
            if context:
                result.append(context)
        return result

    async def find_run_by_idempotency(
        self, tenant_id: str | None, user_id: str | None, idempotency_key: str
    ) -> RunContext | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thread_id, run_id FROM agent_runs "
                "WHERE tenant_id IS ? AND user_id IS ? AND idempotency_key = ? LIMIT 1",
                (tenant_id, user_id, idempotency_key),
            ).fetchone()
        return await self.load_run(row[0], row[1]) if row else None

    async def mark_stale_runs(self) -> list[RunContext]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT thread_id, run_id FROM agent_runs WHERE status IN ('queued', 'running')"
            ).fetchall()
        result: list[RunContext] = []
        for thread_id, run_id in rows:
            context = await self.load_run(thread_id, run_id)
            if context is None:
                continue
            context.status = "interrupted"
            context.emit("run_interrupted", reason="服务重启")
            await self.save_run(context)
            result.append(context)
        return result

    async def save_thread_owner(self, thread_id: str, user_id: str, tenant_id: str) -> None:
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

    async def get_thread_owner(self, thread_id: str) -> tuple[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, tenant_id FROM agent_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
        return tuple(row) if row else None

    async def save_approval(self, approval: ApprovalRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO agent_approvals
                (approval_id, thread_id, run_id, user_id, tenant_id, action,
                 arguments, status, reason, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    status=excluded.status, reason=excluded.reason, expires_at=excluded.expires_at
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
                ),
            )

    async def transition_approval(
        self,
        approval: ApprovalRecord,
        *,
        expected_status: str = "pending",
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE agent_approvals
                SET status=?, reason=?, expires_at=?
                WHERE approval_id=? AND status=?""",
                (
                    approval.status,
                    approval.reason,
                    approval.expires_at,
                    approval.approval_id,
                    expected_status,
                ),
            )
        return cursor.rowcount == 1

    async def load_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT approval_id, thread_id, run_id, user_id, tenant_id,
                action, arguments, status, reason, created_at, expires_at
                FROM agent_approvals WHERE approval_id = ?""",
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        return _approval_from_dict(
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
                    ],
                    row,
                    strict=True,
                )
            )
        )


# ──────────────────────────────────────────────
# 4. PostgreSQL 后端（生产环境）
# ──────────────────────────────────────────────
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
                    status TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, expires_at TEXT
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
            context.status = "interrupted"
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
                    arguments,status,reason,created_at,expires_at
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11)
                ON CONFLICT (approval_id) DO UPDATE SET
                    status=$8, reason=$9, expires_at=$11
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
            )

    async def transition_approval(
        self,
        approval: ApprovalRecord,
        *,
        expected_status: str = "pending",
    ) -> bool:
        self._check()
        async with self._pool.acquire() as conn:
            command = await conn.execute(
                """UPDATE agent_approvals
                SET status=$2, reason=$3, expires_at=$4
                WHERE approval_id=$1 AND status=$5""",
                approval.approval_id,
                approval.status,
                approval.reason,
                approval.expires_at,
                expected_status,
            )
        return command == "UPDATE 1"

    async def load_approval(self, approval_id: str) -> ApprovalRecord | None:
        self._check()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_approvals WHERE approval_id=$1", approval_id)
        return _approval_from_dict(dict(row)) if row else None


# ──────────────────────────────────────────────
# 5. 创建运行时状态存储
# ──────────────────────────────────────────────
def create_runtime_store(
    env: str = "development",
    *,
    sqlite_path: str = "./sessions.db",
    postgres_url: str | None = None,
) -> RuntimeStore:
    """按环境选择运行时状态存储。"""
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryRuntimeStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境需要配置 PostgreSQL 连接地址")
        return PostgresRuntimeStore(postgres_url)
    return SqliteRuntimeStore(sqlite_path)
