"""Chroma 本地持久化向量存储适配器。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from agent_core.access import AccessContext
from agent_core.retrieval._common import (
    decode_record_payload,
    encode_record_payload,
    metadata_matches,
    normalize_record,
    record_accessible,
    validate_vector,
)
from agent_core.retrieval.core import VectorStore
from agent_core.retrieval.types import VectorQuery, VectorRecord, VectorSearchResult

_NAMESPACE_KEY = "_agent_core_namespace"
_USER_KEY = "_agent_core_user_id"
_TENANT_KEY = "_agent_core_tenant_id"
_PAYLOAD_KEY = "_agent_core_payload"
_PUBLIC_OWNER = ""


class ChromaVectorStore(VectorStore):
    """适合本地开发的 Chroma 持久化实现。

    Chroma 的 Python API 是同步接口，因此所有读写都通过 ``asyncio.to_thread``
    执行，避免阻塞 Agent 所在的事件循环。
    """

    def __init__(
        self,
        path: str | Path = "./chroma_db",
        *,
        collection_name: str = "agent_core",
        require_access: bool = False,
        client: Any | None = None,
    ) -> None:
        owns_client = client is None
        if client is None:
            try:
                import chromadb
            except ImportError as exc:
                raise ImportError(
                    "ChromaVectorStore 需要 chromadb，请安装 handwritten-agent-core[chroma]"
                ) from exc
            client = chromadb.PersistentClient(path=str(Path(path).resolve()))
        # Chroma 各版本的 Client/Collection 泛型和重载差异较大，在 SDK 边界收口为 Any。
        self._client: Any = client
        self._owns_client = owns_client
        self._collection: Any = client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._require_access = require_access

    async def close(self) -> None:
        """关闭由当前 Store 创建的 PersistentClient，释放本地文件句柄。"""
        if not self._owns_client:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)
        self._owns_client = False

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
            validate_vector(record.vector)
        await self._check_existing_access([record.record_id for record in values], access)
        metadatas = [self._metadata(record) for record in values]
        await asyncio.to_thread(
            self._collection.upsert,
            ids=[record.record_id for record in values],
            embeddings=[list(record.vector) for record in values],
            documents=[record.document.content for record in values],
            metadatas=metadatas,
        )
        return [record.record_id for record in values]

    async def delete(
        self,
        record_ids: Sequence[str],
        *,
        access: AccessContext | None = None,
    ) -> int:
        self._check_required_access(access)
        if not record_ids:
            return 0
        result = await asyncio.to_thread(
            self._collection.get,
            ids=list(record_ids),
            include=["metadatas"],
        )
        ids = self._flat_list(result, "ids")
        metadatas = self._flat_list(result, "metadatas")
        allowed: list[str] = []
        for index, record_id in enumerate(ids):
            metadata = self._mapping_at(metadatas, index)
            self._check_metadata_access(str(record_id), metadata, access)
            allowed.append(str(record_id))
        if allowed:
            await asyncio.to_thread(self._collection.delete, ids=allowed)
        return len(allowed)

    async def search(self, query: VectorQuery) -> list[VectorSearchResult]:
        self._check_required_access(query.access)
        validate_vector(query.vector)
        count = int(await asyncio.to_thread(self._collection.count))
        if count <= 0:
            return []
        # 没有业务 metadata 过滤时，namespace 和访问范围已下推给 Chroma，
        # 只需取 limit 条；带任意 metadata 过滤时仍全量读取以保持现有精确语义。
        n_results = count if query.metadata_filter else min(count, query.limit)
        result = await asyncio.to_thread(
            self._collection.query,
            query_embeddings=[list(query.vector)],
            n_results=n_results,
            where=self._where(query.namespace, query.access),
            include=["metadatas", "distances", "embeddings"],
        )
        ids = self._first_batch(result, "ids")
        metadatas = self._first_batch(result, "metadatas")
        distances = self._first_batch(result, "distances")
        embeddings = self._first_batch(result, "embeddings")
        matches: list[VectorSearchResult] = []
        for index, record_id in enumerate(ids):
            metadata = self._mapping_at(metadatas, index)
            raw_vector = embeddings[index] if index < len(embeddings) else ()
            vector = tuple(float(cast(Any, value)) for value in cast(Sequence[object], raw_vector))
            record = self._record_from_metadata(str(record_id), metadata, vector)
            combined_metadata = {**record.document.metadata, **record.metadata}
            if not metadata_matches(combined_metadata, query.metadata_filter):
                continue
            if not record_accessible(record, query.access):
                continue
            distance = float(cast(Any, distances[index])) if index < len(distances) else 1.0
            score = 1.0 - distance
            if query.min_score is not None and score < query.min_score:
                continue
            matches.append(VectorSearchResult(record=record, score=score))
            if len(matches) >= query.limit:
                break
        return matches

    async def _check_existing_access(
        self,
        record_ids: Sequence[str],
        access: AccessContext | None,
    ) -> None:
        if access is None:
            return
        result = await asyncio.to_thread(
            self._collection.get,
            ids=list(record_ids),
            include=["metadatas"],
        )
        ids = self._flat_list(result, "ids")
        metadatas = self._flat_list(result, "metadatas")
        for index, record_id in enumerate(ids):
            self._check_metadata_access(
                str(record_id),
                self._mapping_at(metadatas, index),
                access,
            )

    def _check_required_access(self, access: AccessContext | None) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 VectorStore 要求提供 AccessContext")

    @staticmethod
    def _check_metadata_access(
        record_id: str,
        metadata: Mapping[str, object],
        access: AccessContext | None,
    ) -> None:
        if access is None:
            return
        user_id = str(metadata[_USER_KEY]) if metadata.get(_USER_KEY) else None
        tenant_id = str(metadata[_TENANT_KEY]) if metadata.get(_TENANT_KEY) else None
        if not access.can_access(user_id, tenant_id):
            raise PermissionError(f"无权访问向量记录：{record_id}")

    @staticmethod
    def _metadata(record: VectorRecord) -> dict[str, str]:
        return {
            _NAMESPACE_KEY: record.namespace,
            _USER_KEY: record.user_id or _PUBLIC_OWNER,
            _TENANT_KEY: record.tenant_id or _PUBLIC_OWNER,
            _PAYLOAD_KEY: encode_record_payload(record),
        }

    @staticmethod
    def _where(namespace: str, access: AccessContext | None) -> dict[str, object]:
        clauses: list[dict[str, object]] = [{_NAMESPACE_KEY: {"$eq": namespace}}]
        if access is not None:
            clauses.append(
                {
                    "$or": [
                        {_TENANT_KEY: {"$eq": _PUBLIC_OWNER}},
                        {_TENANT_KEY: {"$eq": access.tenant_id}},
                    ]
                }
            )
            if not access.is_admin:
                clauses.append(
                    {
                        "$or": [
                            {_USER_KEY: {"$eq": _PUBLIC_OWNER}},
                            {_USER_KEY: {"$eq": access.user_id}},
                        ]
                    }
                )
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}

    @staticmethod
    def _record_from_metadata(
        record_id: str,
        metadata: Mapping[str, object],
        vector: tuple[float, ...],
    ) -> VectorRecord:
        return decode_record_payload(
            metadata.get(_PAYLOAD_KEY),
            record_id=record_id,
            vector=vector,
            namespace=str(metadata.get(_NAMESPACE_KEY, "default")),
            user_id=str(metadata[_USER_KEY]) if metadata.get(_USER_KEY) else None,
            tenant_id=str(metadata[_TENANT_KEY]) if metadata.get(_TENANT_KEY) else None,
        )

    @staticmethod
    def _flat_list(result: object, key: str) -> list[object]:
        if not isinstance(result, Mapping):
            return []
        return ChromaVectorStore._to_list(result.get(key))

    @staticmethod
    def _first_batch(result: object, key: str) -> list[object]:
        values = ChromaVectorStore._flat_list(result, key)
        if not values:
            return []
        return ChromaVectorStore._to_list(values[0])

    @staticmethod
    def _to_list(value: object) -> list[object]:
        if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
            return []
        return list(value)

    @staticmethod
    def _mapping_at(values: Sequence[object], index: int) -> Mapping[str, object]:
        if index >= len(values) or not isinstance(values[index], Mapping):
            raise ValueError("Chroma 返回的向量记录缺少 metadata")
        return cast(Mapping[str, object], values[index])


__all__ = ["ChromaVectorStore"]
