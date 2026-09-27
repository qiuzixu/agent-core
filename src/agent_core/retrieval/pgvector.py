"""PostgreSQL pgvector 生产向量存储适配器。"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from agent_core.access import AccessContext
from agent_core.retrieval._common import (
    check_record_access,
    decode_record_payload,
    encode_record_payload,
    normalize_record,
    parse_vector,
    validate_vector,
    vector_literal,
)
from agent_core.retrieval.core import VectorStore
from agent_core.retrieval.types import VectorQuery, VectorRecord, VectorSearchResult

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,47}$")


class PgVectorStore(VectorStore):
    """使用 asyncpg 和 PostgreSQL pgvector 扩展的生产实现。"""

    def __init__(
        self,
        dsn: str,
        *,
        dimension: int,
        collection_name: str = "agent_core",
        table_name: str = "agent_vector_records",
        require_access: bool = False,
        create_extension: bool = True,
        create_index: bool = True,
    ) -> None:
        if dimension <= 0:
            raise ValueError("pgvector dimension 必须大于 0")
        if not _IDENTIFIER.fullmatch(table_name):
            raise ValueError("pgvector table_name 只能包含字母、数字和下划线，且最长 48 字符")
        if not collection_name.strip():
            raise ValueError("pgvector collection_name 不能为空")
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._dimension = dimension
        self._collection_name = collection_name
        self._table = f'"{table_name}"'
        self._index = f'"{table_name}_embedding_hnsw"'
        self._require_access = require_access
        self._create_extension = create_extension
        self._create_index = create_index
        self._pool: Any = None

    async def initialize(self) -> None:
        """创建连接池、pgvector 扩展、数据表和余弦 HNSW 索引。"""
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "PgVectorStore 需要 asyncpg，请安装 handwritten-agent-core[pgvector]"
            ) from exc
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as connection:
            if self._create_extension:
                await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {self._table} (
                    collection_name TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    user_id TEXT,
                    tenant_id TEXT,
                    embedding VECTOR({self._dimension}) NOT NULL,
                    payload JSONB NOT NULL,
                    search_metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (collection_name, record_id)
                )"""
            )
            if self._create_index:
                await connection.execute(
                    f"""CREATE INDEX IF NOT EXISTS {self._index}
                    ON {self._table} USING hnsw (embedding vector_cosine_ops)"""
                )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def upsert(
        self,
        records: Sequence[VectorRecord],
        *,
        access: AccessContext | None = None,
    ) -> list[str]:
        values = [normalize_record(record, access, require_access=self._require_access) for record in records]
        if not values:
            return []
        for record in values:
            validate_vector(record.vector, dimension=self._dimension)
        self._check_initialized()
        ids = [record.record_id for record in values]
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                if access is not None:
                    rows = await connection.fetch(
                        f"""SELECT record_id, namespace, user_id, tenant_id, embedding::text AS embedding,
                            payload FROM {self._table}
                        WHERE collection_name=$1 AND record_id=ANY($2::text[])
                        FOR UPDATE""",
                        self._collection_name,
                        ids,
                    )
                    for row in rows:
                        check_record_access(self._row_to_record(row), access)
                await connection.executemany(
                    f"""INSERT INTO {self._table} (
                        collection_name, record_id, namespace, user_id, tenant_id,
                        embedding, payload, search_metadata
                    ) VALUES ($1,$2,$3,$4,$5,$6::vector,$7::jsonb,$8::jsonb)
                    ON CONFLICT (collection_name, record_id) DO UPDATE SET
                        namespace=EXCLUDED.namespace,
                        user_id=EXCLUDED.user_id,
                        tenant_id=EXCLUDED.tenant_id,
                        embedding=EXCLUDED.embedding,
                        payload=EXCLUDED.payload,
                        search_metadata=EXCLUDED.search_metadata,
                        updated_at=NOW()""",
                    [
                        (
                            self._collection_name,
                            record.record_id,
                            record.namespace,
                            record.user_id,
                            record.tenant_id,
                            vector_literal(record.vector),
                            encode_record_payload(record),
                            self._json({**record.document.metadata, **record.metadata}),
                        )
                        for record in values
                    ],
                )
        return ids

    async def delete(
        self,
        record_ids: Sequence[str],
        *,
        access: AccessContext | None = None,
    ) -> int:
        self._check_required_access(access)
        if not record_ids:
            return 0
        self._check_initialized()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                existing = await connection.fetch(
                    f"""SELECT record_id, user_id, tenant_id FROM {self._table}
                    WHERE collection_name=$1 AND record_id=ANY($2::text[])
                    FOR UPDATE""",
                    self._collection_name,
                    list(record_ids),
                )
                if access is not None:
                    for row in existing:
                        if not access.can_access(row["user_id"], row["tenant_id"]):
                            raise PermissionError(f"无权访问向量记录：{row['record_id']}")
                rows = await connection.fetch(
                    f"""DELETE FROM {self._table}
                    WHERE collection_name=$1 AND record_id=ANY($2::text[])
                    RETURNING record_id""",
                    self._collection_name,
                    list(record_ids),
                )
        return len(rows)

    async def search(self, query: VectorQuery) -> list[VectorSearchResult]:
        self._check_required_access(query.access)
        validate_vector(query.vector, dimension=self._dimension)
        self._check_initialized()
        access = query.access
        has_access = access is not None
        rows = []
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT record_id, namespace, user_id, tenant_id,
                    embedding::text AS embedding, payload,
                    1 - (embedding <=> $1::vector) AS score
                FROM {self._table}
                WHERE collection_name=$2 AND namespace=$3
                  AND ($4::boolean=FALSE OR tenant_id IS NULL OR tenant_id=$5)
                  AND ($4::boolean=FALSE OR $6::boolean=TRUE OR user_id IS NULL OR user_id=$7)
                  AND search_metadata @> $8::jsonb
                  AND ($9::double precision IS NULL OR 1 - (embedding <=> $1::vector) >= $9)
                ORDER BY embedding <=> $1::vector, record_id
                LIMIT $10""",
                vector_literal(query.vector),
                self._collection_name,
                query.namespace,
                has_access,
                access.tenant_id if access else None,
                access.is_admin if access else False,
                access.user_id if access else None,
                self._json(query.metadata_filter),
                query.min_score,
                query.limit,
            )
        return [
            VectorSearchResult(record=self._row_to_record(row), score=float(row["score"]))
            for row in rows
        ]

    def _row_to_record(self, row: Any) -> VectorRecord:
        return decode_record_payload(
            row["payload"],
            record_id=str(row["record_id"]),
            vector=parse_vector(row["embedding"]),
            namespace=str(row["namespace"]),
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
        )

    def _check_initialized(self) -> None:
        if self._pool is None:
            raise RuntimeError("PgVectorStore 未初始化，请先调用 initialize()")

    def _check_required_access(self, access: AccessContext | None) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 VectorStore 要求提供 AccessContext")

    @staticmethod
    def _json(value: dict[str, Any]) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("向量 metadata 必须可以序列化为 JSON") from exc


__all__ = ["PgVectorStore"]
