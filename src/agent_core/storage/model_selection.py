"""用户/租户级模型选择存储。

模型选择属于 Agent Core 的运行配置，不应放在前端 localStorage 或进程全局变量中。
开发环境使用 SQLite，测试使用内存，生产环境使用 PostgreSQL。
"""

from __future__ import annotations

import asyncio
import copy
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.ports.model_selection import (
    ModelSelection,
    ModelSelectionScope,
    ModelSelectionStore,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_scope(scope: ModelSelectionScope, user_id: str | None, tenant_id: str | None) -> None:
    if scope == "user" and not user_id:
        raise ValueError("用户级模型选择需要 user_id")
    if not tenant_id:
        raise ValueError("模型选择需要 tenant_id")


class MemoryModelSelectionStore:
    """测试环境内存实现。"""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str | None, str], ModelSelection] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, *, user_id: str | None, tenant_id: str | None) -> ModelSelection | None:
        async with self._lock:
            if user_id and tenant_id:
                value = self._data.get(("user", user_id, tenant_id))
                if value:
                    return copy.deepcopy(value)
            if tenant_id:
                value = self._data.get(("tenant", "", tenant_id))
                if value:
                    return copy.deepcopy(value)
            return None

    async def save(
        self,
        provider: str,
        model: str,
        *,
        scope: ModelSelectionScope,
        user_id: str | None,
        tenant_id: str | None,
    ) -> ModelSelection:
        _validate_scope(scope, user_id, tenant_id)
        async with self._lock:
            key = (scope, user_id if scope == "user" else "", tenant_id or "")
            previous = self._data.get(key)
            value = ModelSelection(
                provider=provider.strip().lower(),
                model=model.strip(),
                scope=scope,
                user_id=user_id if scope == "user" else None,
                tenant_id=tenant_id,
                version=(previous.version + 1) if previous else 1,
                updated_at=_now(),
            )
            self._data[key] = value
            return copy.deepcopy(value)

    async def clear(self, *, scope: ModelSelectionScope, user_id: str | None, tenant_id: str | None) -> None:
        _validate_scope(scope, user_id, tenant_id)
        async with self._lock:
            self._data.pop((scope, user_id if scope == "user" else "", tenant_id or ""), None)


class SqliteModelSelectionStore:
    """开发环境 SQLite 实现。"""

    def __init__(self, db_path: str | Path = "./sessions.db") -> None:
        self._db_path = str(Path(db_path).resolve())
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_model_selections (
                    scope TEXT NOT NULL CHECK(scope IN ('user', 'tenant')),
                    user_id TEXT,
                    tenant_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (scope, user_id, tenant_id)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_model_selection_tenant "
                "ON agent_model_selections(tenant_id)"
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

    @staticmethod
    def _row(row: sqlite3.Row | tuple[Any, ...] | None) -> ModelSelection | None:
        if row is None:
            return None
        values = tuple(row)
        return ModelSelection(
            provider=str(values[3]),
            model=str(values[4]),
            scope=str(values[0]),
            user_id=values[1] or None,
            tenant_id=values[2],
            version=int(values[5]),
            updated_at=str(values[6]),
        )

    async def resolve(self, *, user_id: str | None, tenant_id: str | None) -> ModelSelection | None:
        with self._connect() as conn:
            if user_id and tenant_id:
                row = conn.execute(
                    "SELECT scope,user_id,tenant_id,provider,model,version,updated_at "
                    "FROM agent_model_selections WHERE scope='user' AND user_id=? AND tenant_id=?",
                    (user_id, tenant_id),
                ).fetchone()
                value = self._row(row)
                if value:
                    return value
            if tenant_id:
                row = conn.execute(
                    "SELECT scope,user_id,tenant_id,provider,model,version,updated_at "
                    "FROM agent_model_selections WHERE scope='tenant' AND tenant_id=?",
                    (tenant_id,),
                ).fetchone()
                return self._row(row)
        return None

    async def save(
        self,
        provider: str,
        model: str,
        *,
        scope: ModelSelectionScope,
        user_id: str | None,
        tenant_id: str | None,
    ) -> ModelSelection:
        _validate_scope(scope, user_id, tenant_id)
        key_user = user_id if scope == "user" else ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version FROM agent_model_selections WHERE scope=? AND user_id IS ? AND tenant_id=?",
                (scope, key_user, tenant_id),
            ).fetchone()
            version = int(row[0]) + 1 if row else 1
            value = ModelSelection(
                provider.strip().lower(), model.strip(), scope, key_user, tenant_id, version, _now()
            )
            conn.execute(
                """INSERT INTO agent_model_selections(
                    scope,user_id,tenant_id,provider,model,version,updated_at
                )
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(scope,user_id,tenant_id) DO UPDATE SET
                provider=excluded.provider, model=excluded.model, version=excluded.version,
                updated_at=excluded.updated_at""",
                (scope, key_user, tenant_id, value.provider, value.model, value.version, value.updated_at),
            )
            return value

    async def clear(self, *, scope: ModelSelectionScope, user_id: str | None, tenant_id: str | None) -> None:
        _validate_scope(scope, user_id, tenant_id)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM agent_model_selections WHERE scope=? AND user_id IS ? AND tenant_id=?",
                (scope, user_id if scope == "user" else "", tenant_id),
            )


class PostgresModelSelectionStore:
    """生产环境 PostgreSQL 实现。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any = None

    async def initialize(self) -> None:
        try:
            import asyncpg  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError("PostgresModelSelectionStore 需要 asyncpg") from exc
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute("""CREATE TABLE IF NOT EXISTS agent_model_selections (
                scope TEXT NOT NULL CHECK(scope IN ('user', 'tenant')),
                user_id TEXT, tenant_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(scope, user_id, tenant_id)
            )""")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresModelSelectionStore 未初始化")

    @staticmethod
    def _value(row: Any) -> ModelSelection:
        return ModelSelection(
            provider=str(row["provider"]),
            model=str(row["model"]),
            scope=str(row["scope"]),
            user_id=row["user_id"] or None,
            tenant_id=row["tenant_id"],
            version=int(row["version"]),
            updated_at=str(row["updated_at"]),
        )

    async def resolve(self, *, user_id: str | None, tenant_id: str | None) -> ModelSelection | None:
        self._check()
        async with self._pool.acquire() as conn:
            if user_id and tenant_id:
                row = await conn.fetchrow(
                    "SELECT * FROM agent_model_selections WHERE scope='user' AND user_id=$1 AND tenant_id=$2",
                    user_id,
                    tenant_id,
                )
                if row:
                    return self._value(row)
            if tenant_id:
                row = await conn.fetchrow(
                    "SELECT * FROM agent_model_selections WHERE scope='tenant' AND tenant_id=$1",
                    tenant_id,
                )
                return self._value(row) if row else None
        return None

    async def save(
        self,
        provider: str,
        model: str,
        *,
        scope: ModelSelectionScope,
        user_id: str | None,
        tenant_id: str | None,
    ) -> ModelSelection:
        _validate_scope(scope, user_id, tenant_id)
        self._check()
        key_user = user_id if scope == "user" else ""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO agent_model_selections(
                    scope,user_id,tenant_id,provider,model,version,updated_at
                )
                VALUES($1,$2,$3,$4,$5,1,NOW())
                ON CONFLICT(scope,user_id,tenant_id) DO UPDATE SET
                    provider=EXCLUDED.provider,
                    model=EXCLUDED.model,
                    version=agent_model_selections.version + 1,
                    updated_at=NOW()
                RETURNING scope,user_id,tenant_id,provider,model,version,updated_at""",
                scope,
                key_user,
                tenant_id,
                provider.strip().lower(),
                model.strip(),
            )
            return self._value(row)

    async def clear(self, *, scope: ModelSelectionScope, user_id: str | None, tenant_id: str | None) -> None:
        _validate_scope(scope, user_id, tenant_id)
        self._check()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """DELETE FROM agent_model_selections
                WHERE scope=$1 AND user_id IS NOT DISTINCT FROM $2 AND tenant_id=$3""",
                scope,
                user_id if scope == "user" else "",
                tenant_id,
            )


def create_model_selection_store(
    env: str = "development",
    *,
    sqlite_path: str = "./sessions.db",
    postgres_url: str | None = None,
) -> ModelSelectionStore:
    """按环境创建模型选择存储。"""
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryModelSelectionStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境需要配置 PostgreSQL 连接地址")
        return PostgresModelSelectionStore(postgres_url)
    return SqliteModelSelectionStore(sqlite_path)


__all__ = [
    "MemoryModelSelectionStore",
    "ModelSelection",
    "ModelSelectionScope",
    "ModelSelectionStore",
    "PostgresModelSelectionStore",
    "SqliteModelSelectionStore",
    "create_model_selection_store",
]
