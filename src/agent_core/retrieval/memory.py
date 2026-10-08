"""用于开发、测试和小数据集的零依赖检索实现。"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from agent_core.access import AccessContext
from agent_core.documents import Document
from agent_core.retrieval._common import check_record_access, normalize_record, validate_vector
from agent_core.retrieval.core import Embeddings, VectorStore
from agent_core.retrieval.types import (
    RetrievalQuery,
    RetrievalResult,
    SearchType,
    VectorQuery,
    VectorRecord,
    VectorSearchResult,
)


def _metadata_matches(metadata: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(metadata.get(key) == value for key, value in expected.items())


def _cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        raise ValueError(f"向量维度不一致：{len(left)} != {len(right)}")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


class InMemoryVectorStore(VectorStore):
    """带 namespace、metadata 和用户/租户过滤的内存向量存储。"""

    def __init__(self, *, require_access: bool = False) -> None:
        self._records: dict[str, VectorRecord] = {}
        self._dimension: int | None = None
        self._require_access = require_access

    async def upsert(
        self,
        records: Sequence[VectorRecord],
        *,
        access: AccessContext | None = None,
    ) -> list[str]:
        self._check_required_access(access)
        saved: list[str] = []
        for record in records:
            self._check_dimension(record.vector)
            value = normalize_record(record, access, require_access=self._require_access)
            existing = self._records.get(value.record_id)
            if existing is not None:
                check_record_access(existing, access)
            self._records[value.record_id] = value
            saved.append(value.record_id)
        return saved

    async def delete(
        self,
        record_ids: Sequence[str],
        *,
        access: AccessContext | None = None,
    ) -> int:
        self._check_required_access(access)
        existing = [self._records[record_id] for record_id in record_ids if record_id in self._records]
        # 先完成整批权限校验，避免中途失败时前面的记录已经被删除。
        for record in existing:
            self._check_record_access(record, access)
        deleted = 0
        for record_id in record_ids:
            record = self._records.get(record_id)
            if record is None:
                continue
            del self._records[record_id]
            deleted += 1
        return deleted

    async def search(self, query: VectorQuery) -> list[VectorSearchResult]:
        self._check_required_access(query.access)
        if self._dimension is not None and len(query.vector) != self._dimension:
            raise ValueError(f"向量维度不一致：期望 {self._dimension}，实际 {len(query.vector)}")
        matches: list[VectorSearchResult] = []
        for record in self._records.values():
            if record.namespace != query.namespace:
                continue
            if not _metadata_matches({**record.document.metadata, **record.metadata}, query.metadata_filter):
                continue
            if not self._record_accessible(record, query.access):
                continue
            score = _cosine_similarity(query.vector, record.vector)
            if query.min_score is not None and score < query.min_score:
                continue
            matches.append(VectorSearchResult(record=record, score=score))
        matches.sort(key=lambda item: (-item.score, item.record.record_id))
        return matches[: query.limit]

    def _check_dimension(self, vector: tuple[float, ...]) -> None:
        validate_vector(vector)
        if self._dimension is None:
            self._dimension = len(vector)
        elif len(vector) != self._dimension:
            raise ValueError(f"向量维度不一致：期望 {self._dimension}，实际 {len(vector)}")

    def _check_required_access(self, access: AccessContext | None) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 VectorStore 要求提供 AccessContext")

    @staticmethod
    def _record_accessible(record: VectorRecord, access: AccessContext | None) -> bool:
        return access is None or access.can_access(record.user_id, record.tenant_id)

    @classmethod
    def _check_record_access(cls, record: VectorRecord, access: AccessContext | None) -> None:
        if not cls._record_accessible(record, access):
            raise PermissionError(f"无权访问向量记录：{record.record_id}")


class EmbeddingRetriever:
    """组合 Embeddings 和 VectorStore 的通用语义 Retriever。"""

    def __init__(self, embeddings: Embeddings, vector_store: VectorStore) -> None:
        self._embeddings = embeddings
        self._vector_store = vector_store

    async def add_documents(
        self,
        documents: Sequence[Document],
        *,
        namespace: str = "default",
        access: AccessContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        vectors = await self._embeddings.embed_documents([item.content for item in documents])
        if len(vectors) != len(documents):
            raise ValueError("Embedding 返回数量与文档数量不一致")
        records = [
            VectorRecord(
                record_id=document.document_id,
                vector=tuple(float(value) for value in vector),
                document=document,
                namespace=namespace,
                user_id=access.user_id if access else None,
                tenant_id=access.tenant_id if access else None,
                metadata=dict(metadata or {}),
            )
            for document, vector in zip(documents, vectors, strict=True)
        ]
        return await self._vector_store.upsert(records, access=access)

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        if query.search_type != SearchType.SIMILARITY:
            raise ValueError("EmbeddingRetriever 当前只支持 similarity 检索")
        vector = await self._embeddings.embed_query(query.text)
        matches = await self._vector_store.search(
            VectorQuery(
                vector=tuple(float(value) for value in vector),
                limit=query.limit,
                namespace=query.namespace,
                metadata_filter=query.metadata_filter,
                min_score=query.min_score,
                access=query.access,
            )
        )
        return [
            RetrievalResult(
                document=item.record.document,
                score=item.score,
                rank=index,
                metadata={"record_id": item.record.record_id, **item.record.metadata},
            )
            for index, item in enumerate(matches, start=1)
        ]


class KeywordRetriever:
    """零依赖关键词 Retriever，适合测试和小型静态语料。"""

    def __init__(
        self,
        documents: Iterable[Document] | None = None,
        *,
        require_access: bool = False,
    ) -> None:
        self._documents = list(documents or ())
        self._require_access = require_access

    def add_documents(self, documents: Iterable[Document]) -> None:
        self._documents.extend(documents)

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        if query.search_type != SearchType.KEYWORD:
            raise ValueError("KeywordRetriever 只支持 keyword 检索")
        if self._require_access and query.access is None:
            raise PermissionError("该 Retriever 要求提供 AccessContext")
        terms = [term.casefold() for term in query.text.split() if term.strip()]
        if not terms and query.text.strip():
            terms = [query.text.strip().casefold()]
        results: list[RetrievalResult] = []
        for document in self._documents:
            metadata = document.metadata
            if metadata.get("namespace", "default") != query.namespace:
                continue
            if not _metadata_matches(metadata, query.metadata_filter):
                continue
            if query.access is not None and not query.access.can_access(
                metadata.get("user_id"), metadata.get("tenant_id")
            ):
                continue
            content = document.content.casefold()
            matches = sum(content.count(term) for term in terms)
            if matches == 0:
                continue
            score = matches / max(len(terms), 1)
            if query.min_score is not None and score < query.min_score:
                continue
            results.append(
                RetrievalResult(
                    document=document,
                    score=score,
                    rank=0,
                    metadata={"matches": matches},
                )
            )
        results.sort(key=lambda item: (-item.score, item.document.document_id))
        return [replace(item, rank=index) for index, item in enumerate(results[: query.limit], start=1)]
