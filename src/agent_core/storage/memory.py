"""长期记忆的内存、SQLite 和 PostgreSQL 实现。"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from agent_core.access import AccessContext
from agent_core.errors import MemoryConflictError
from agent_core.ports.memory import MemoryRecord, MemorySearchResult, MemoryStore, memory_now
from agent_core.storage._sqlite_utils import (
    _run_in_thread,
    run_sync_in_thread,
    sqlite_connection,
)

_WORD = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _postgres_datetime(value: str | None) -> datetime | None:
    """把 Core 使用的 ISO 时间转换为 asyncpg 接受的 datetime。"""

    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class _MemoryAccessMixin:
    def __init__(self, *, require_access: bool = False) -> None:
        self._strict_access = require_access

    def _require_access(self, access: AccessContext | None) -> None:
        if self._strict_access and access is None:
            raise PermissionError("该 MemoryStore 要求提供 AccessContext")

    def _can_access(self, record: MemoryRecord, access: AccessContext | None) -> bool:
        self._require_access(access)
        return access is None or access.can_access(record.user_id, record.tenant_id)

    def _bind(self, record: MemoryRecord, access: AccessContext | None) -> MemoryRecord:
        value = MemoryRecord.from_dict(copy.deepcopy(record.to_dict()))
        self._require_access(access)
        if access is None:
            return value
        if value.user_id is None:
            value.user_id = access.user_id
        if value.tenant_id is None:
            value.tenant_id = access.tenant_id
        if not access.can_access(value.user_id, value.tenant_id):
            raise PermissionError("不能把记忆写入其他用户或租户")
        return value

    @staticmethod
    def _score(record: MemoryRecord, query: str) -> float:
        words = [item.lower() for item in _WORD.findall(query)]
        if not words:
            return 1.0
        haystack = f"{record.content} {json.dumps(record.metadata, ensure_ascii=False)}".lower()
        hits = sum(haystack.count(word) for word in words)
        return hits / len(words)

    @staticmethod
    def _metadata_matches(record: MemoryRecord, filters: dict[str, Any] | None) -> bool:
        return not filters or all(record.metadata.get(key) == value for key, value in filters.items())


class InMemoryMemoryStore(_MemoryAccessMixin):
    """测试和单进程运行使用的跨会话记忆存储。"""

    def __init__(self, *, require_access: bool = False) -> None:
        super().__init__(require_access=require_access)
        self._items: dict[str, MemoryRecord] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        record: MemoryRecord,
        *,
        expected_version: int | None = None,
        access: AccessContext | None = None,
    ) -> MemoryRecord:
        async with self._lock:
            value = self._bind(record, access)
            current = self._items.get(value.memory_id)
            if current is not None and not self._can_access(current, access):
                raise PermissionError("无权更新该记忆")
            actual = current.version if current else 0
            if expected_version is not None and expected_version != actual:
                raise MemoryConflictError(value.memory_id, expected_version, actual)
            value.version = actual + 1
            value.created_at = current.created_at if current else value.created_at
            value.updated_at = memory_now()
            self._items[value.memory_id] = value
            return MemoryRecord.from_dict(copy.deepcopy(value.to_dict()))

    async def get(
        self,
        memory_id: str,
        *,
        access: AccessContext | None = None,
    ) -> MemoryRecord | None:
        async with self._lock:
            self._require_access(access)
            value = self._items.get(memory_id)
            if value is None:
                return None
            if value.is_expired():
                self._items.pop(memory_id, None)
                return None
            if not self._can_access(value, access):
                raise PermissionError("无权访问该记忆")
            return MemoryRecord.from_dict(copy.deepcopy(value.to_dict()))

    async def delete(
        self,
        memory_id: str,
        *,
        expected_version: int | None = None,
        access: AccessContext | None = None,
    ) -> bool:
        async with self._lock:
            self._require_access(access)
            current = self._items.get(memory_id)
            if current is None:
                return False
            if not self._can_access(current, access):
                raise PermissionError("无权删除该记忆")
            if expected_version is not None and expected_version != current.version:
                raise MemoryConflictError(memory_id, expected_version, current.version)
            del self._items[memory_id]
            return True

    async def search(
        self,
        namespace: str,
        *,
        query: str = "",
        metadata: dict[str, Any] | None = None,
        limit: int = 20,
        access: AccessContext | None = None,
    ) -> list[MemorySearchResult]:
        if limit <= 0:
            return []
        async with self._lock:
            self._require_access(access)
            expired = [key for key, item in self._items.items() if item.is_expired()]
            for key in expired:
                self._items.pop(key, None)
            results = []
            for item in self._items.values():
                if item.namespace != namespace or not self._can_access(item, access):
                    continue
                if not self._metadata_matches(item, metadata):
                    continue
                score = self._score(item, query)
                if query and score <= 0:
                    continue
                results.append(
                    MemorySearchResult(
                        record=MemoryRecord.from_dict(copy.deepcopy(item.to_dict())),
                        score=score,
                    )
                )
            results.sort(key=lambda item: (item.score, item.record.updated_at), reverse=True)
            return results[:limit]


class SqliteMemoryStore(_MemoryAccessMixin):
    """本地开发使用的 SQLite 长期记忆存储。"""

    DDL = """
    CREATE TABLE IF NOT EXISTS agent_memories (
        memory_id TEXT PRIMARY KEY,
        namespace TEXT NOT NULL,
        memory_key TEXT,
        content TEXT NOT NULL,
        user_id TEXT,
        tenant_id TEXT,
        source TEXT,
        metadata TEXT NOT NULL DEFAULT '{}',
        expires_at TEXT,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_agent_memories_namespace
    ON agent_memories(namespace, tenant_id, user_id, updated_at);
    """

    def __init__(self, db_path: str | Path = "./sessions.db", *, require_access: bool = False) -> None:
        super().__init__(require_access=require_access)
        self._db_path = str(Path(db_path).resolve())
        # 建表 DDL/PRAGMA 初始化同样放入工作线程执行，与 async 方法共用同一连接参数规范。
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
    def _row(row: tuple[Any, ...]) -> MemoryRecord:
        return MemoryRecord(
            memory_id=str(row[0]),
            namespace=str(row[1]),
            key=row[2],
            content=str(row[3]),
            user_id=row[4],
            tenant_id=row[5],
            source=row[6],
            metadata=json.loads(row[7] or "{}"),
            expires_at=row[8],
            version=int(row[9]),
            created_at=str(row[10]),
            updated_at=str(row[11]),
        )

    async def put(
        self,
        record: MemoryRecord,
        *,
        expected_version: int | None = None,
        access: AccessContext | None = None,
    ) -> MemoryRecord:
        value = self._bind(record, access)

        def _op() -> MemoryRecord:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT memory_id,namespace,memory_key,content,user_id,tenant_id,source,metadata,"
                    "expires_at,version,created_at,updated_at FROM agent_memories WHERE memory_id=?",
                    (value.memory_id,),
                ).fetchone()
                current = self._row(row) if row else None
                if current is not None and not self._can_access(current, access):
                    raise PermissionError("无权更新该记忆")
                actual = current.version if current else 0
                if expected_version is not None and expected_version != actual:
                    raise MemoryConflictError(value.memory_id, expected_version, actual)
                value.version = actual + 1
                value.created_at = current.created_at if current else value.created_at
                value.updated_at = memory_now()
                payload = (
                    value.namespace,
                    value.key,
                    value.content,
                    value.user_id,
                    value.tenant_id,
                    value.source,
                    json.dumps(value.metadata, ensure_ascii=False),
                    value.expires_at,
                    value.version,
                    value.created_at,
                    value.updated_at,
                )
                if current is None:
                    try:
                        conn.execute(
                            "INSERT INTO agent_memories(memory_id,namespace,memory_key,content,"
                            "user_id,tenant_id,source,metadata,expires_at,version,created_at,updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (value.memory_id, *payload),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise MemoryConflictError(value.memory_id, 0, 1) from exc
                else:
                    changed = conn.execute(
                        "UPDATE agent_memories SET namespace=?,memory_key=?,content=?,user_id=?,tenant_id=?,"
                        "source=?,metadata=?,expires_at=?,version=?,created_at=?,updated_at=? "
                        "WHERE memory_id=? AND version=?",
                        (*payload, value.memory_id, actual),
                    ).rowcount
                    if changed != 1:
                        raise MemoryConflictError(value.memory_id, actual, actual + 1)
            return MemoryRecord.from_dict(value.to_dict())

        return await _run_in_thread(_op)

    async def get(
        self,
        memory_id: str,
        *,
        access: AccessContext | None = None,
    ) -> MemoryRecord | None:
        self._require_access(access)

        def _op() -> MemoryRecord | None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT memory_id,namespace,memory_key,content,user_id,tenant_id,source,metadata,"
                    "expires_at,version,created_at,updated_at FROM agent_memories WHERE memory_id=?",
                    (memory_id,),
                ).fetchone()
                if row is None:
                    return None
                value = self._row(row)
                if value.is_expired():
                    conn.execute("DELETE FROM agent_memories WHERE memory_id=?", (memory_id,))
                    return None
                return value

        value = await _run_in_thread(_op)
        if value is None:
            return None
        if not self._can_access(value, access):
            raise PermissionError("无权访问该记忆")
        return value

    async def delete(
        self,
        memory_id: str,
        *,
        expected_version: int | None = None,
        access: AccessContext | None = None,
    ) -> bool:
        current = await self.get(memory_id, access=access)
        if current is None:
            return False
        if expected_version is not None and expected_version != current.version:
            raise MemoryConflictError(memory_id, expected_version, current.version)

        def _op() -> int:
            with self._connect() as conn:
                return conn.execute(
                    "DELETE FROM agent_memories WHERE memory_id=? AND version=?",
                    (memory_id, current.version),
                ).rowcount

        changed = await _run_in_thread(_op)
        if changed != 1:
            raise MemoryConflictError(memory_id, current.version, current.version + 1)
        return True

    async def search(
        self,
        namespace: str,
        *,
        query: str = "",
        metadata: dict[str, Any] | None = None,
        limit: int = 20,
        access: AccessContext | None = None,
    ) -> list[MemorySearchResult]:
        self._require_access(access)
        if limit <= 0:
            return []

        def _op() -> list[Any]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT memory_id,namespace,memory_key,content,user_id,tenant_id,source,metadata,"
                    "expires_at,version,created_at,updated_at FROM agent_memories WHERE namespace=?",
                    (namespace,),
                ).fetchall()

        rows = await _run_in_thread(_op)
        results = []
        for row in rows:
            value = self._row(row)
            if value.is_expired() or not self._can_access(value, access):
                continue
            if not self._metadata_matches(value, metadata):
                continue
            score = self._score(value, query)
            if query and score <= 0:
                continue
            results.append(MemorySearchResult(value, score))
        results.sort(key=lambda item: (item.score, item.record.updated_at), reverse=True)
        return results[:limit]


class PostgresMemoryStore(_MemoryAccessMixin):
    """生产环境使用的 PostgreSQL 长期记忆存储。"""

    def __init__(self, dsn: str, *, require_access: bool = False) -> None:
        super().__init__(require_access=require_access)
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any = None

    async def initialize(self) -> None:
        try:
            asyncpg = import_module("asyncpg")
        except ImportError as exc:
            raise ImportError("PostgresMemoryStore 需要 asyncpg") from exc
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_memories (
                    memory_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    memory_key TEXT,
                    content TEXT NOT NULL,
                    user_id TEXT,
                    tenant_id TEXT,
                    source TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    expires_at TIMESTAMPTZ,
                    version INTEGER NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_memories_namespace
                ON agent_memories(namespace, tenant_id, user_id, updated_at)
            """)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresMemoryStore 未初始化")

    @staticmethod
    def _row(row: Any) -> MemoryRecord:
        value = dict(row)
        metadata = value.get("metadata")
        value["metadata"] = json.loads(metadata) if isinstance(metadata, str) else dict(metadata or {})
        for name in ("expires_at", "created_at", "updated_at"):
            if value.get(name) is not None and not isinstance(value[name], str):
                value[name] = value[name].isoformat()
        value["key"] = value.pop("memory_key", None)
        return MemoryRecord.from_dict(value)

    async def put(
        self,
        record: MemoryRecord,
        *,
        expected_version: int | None = None,
        access: AccessContext | None = None,
    ) -> MemoryRecord:
        self._check()
        value = self._bind(record, access)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM agent_memories WHERE memory_id=$1", value.memory_id)
                current = self._row(row) if row else None
                if current is not None and not self._can_access(current, access):
                    raise PermissionError("无权更新该记忆")
                actual = current.version if current else 0
                if expected_version is not None and expected_version != actual:
                    raise MemoryConflictError(value.memory_id, expected_version, actual)
                value.version = actual + 1
                value.created_at = current.created_at if current else value.created_at
                value.updated_at = memory_now()
                if current is None:
                    try:
                        await conn.execute(
                            "INSERT INTO agent_memories(memory_id,namespace,memory_key,content,"
                            "user_id,tenant_id,"
                            "source,metadata,expires_at,version,created_at,updated_at) "
                            "VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12)",
                            value.memory_id,
                            value.namespace,
                            value.key,
                            value.content,
                            value.user_id,
                            value.tenant_id,
                            value.source,
                            json.dumps(value.metadata, ensure_ascii=False),
                            _postgres_datetime(value.expires_at),
                            value.version,
                            _postgres_datetime(value.created_at),
                            _postgres_datetime(value.updated_at),
                        )
                    except Exception as exc:
                        if getattr(exc, "sqlstate", None) == "23505":
                            raise MemoryConflictError(value.memory_id, 0, 1) from exc
                        raise
                else:
                    status = await conn.execute(
                        "UPDATE agent_memories SET namespace=$2,memory_key=$3,content=$4,user_id=$5,"
                        "tenant_id=$6,source=$7,metadata=$8::jsonb,expires_at=$9,version=$10,"
                        "updated_at=$11 WHERE memory_id=$1 AND version=$12",
                        value.memory_id,
                        value.namespace,
                        value.key,
                        value.content,
                        value.user_id,
                        value.tenant_id,
                        value.source,
                        json.dumps(value.metadata, ensure_ascii=False),
                        _postgres_datetime(value.expires_at),
                        value.version,
                        _postgres_datetime(value.updated_at),
                        actual,
                    )
                    if not status.endswith(" 1"):
                        raise MemoryConflictError(value.memory_id, actual, actual + 1)
        return MemoryRecord.from_dict(value.to_dict())

    async def get(
        self,
        memory_id: str,
        *,
        access: AccessContext | None = None,
    ) -> MemoryRecord | None:
        self._check()
        self._require_access(access)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_memories WHERE memory_id=$1", memory_id)
            if row is None:
                return None
            value = self._row(row)
            if value.is_expired():
                await conn.execute("DELETE FROM agent_memories WHERE memory_id=$1", memory_id)
                return None
        if not self._can_access(value, access):
            raise PermissionError("无权访问该记忆")
        return value

    async def delete(
        self,
        memory_id: str,
        *,
        expected_version: int | None = None,
        access: AccessContext | None = None,
    ) -> bool:
        current = await self.get(memory_id, access=access)
        if current is None:
            return False
        if expected_version is not None and expected_version != current.version:
            raise MemoryConflictError(memory_id, expected_version, current.version)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM agent_memories WHERE memory_id=$1 AND version=$2",
                memory_id,
                current.version,
            )
        if not status.endswith(" 1"):
            raise MemoryConflictError(memory_id, current.version, current.version + 1)
        return True

    async def search(
        self,
        namespace: str,
        *,
        query: str = "",
        metadata: dict[str, Any] | None = None,
        limit: int = 20,
        access: AccessContext | None = None,
    ) -> list[MemorySearchResult]:
        self._check()
        self._require_access(access)
        if limit <= 0:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agent_memories WHERE namespace=$1 "
                "AND (expires_at IS NULL OR expires_at > NOW()) ORDER BY updated_at DESC",
                namespace,
            )
        results = []
        for row in rows:
            value = self._row(row)
            if not self._can_access(value, access) or not self._metadata_matches(value, metadata):
                continue
            score = self._score(value, query)
            if query and score <= 0:
                continue
            results.append(MemorySearchResult(value, score))
        results.sort(key=lambda item: (item.score, item.record.updated_at), reverse=True)
        return results[:limit]


def create_memory_store(
    env: str = "development",
    *,
    sqlite_path: str = "./sessions.db",
    postgres_url: str | None = None,
    require_access: bool = False,
) -> MemoryStore:
    """按环境创建长期记忆存储。"""

    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return InMemoryMemoryStore(require_access=require_access)
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境需要配置 PostgreSQL 连接地址")
        return PostgresMemoryStore(postgres_url, require_access=require_access)
    return SqliteMemoryStore(sqlite_path, require_access=require_access)


__all__ = [
    "InMemoryMemoryStore",
    "PostgresMemoryStore",
    "SqliteMemoryStore",
    "create_memory_store",
]
