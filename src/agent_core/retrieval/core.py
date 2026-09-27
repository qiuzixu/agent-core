"""检索依赖倒置协议。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from agent_core.access import AccessContext
from agent_core.retrieval.types import (
    RetrievalQuery,
    RetrievalResult,
    VectorQuery,
    VectorRecord,
    VectorSearchResult,
)


@runtime_checkable
class Embeddings(Protocol):
    """文本向量化协议，厂商 SDK 由扩展适配器实现。"""

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class Retriever(Protocol):
    """根据查询返回排好序的文档。"""

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]: ...


@runtime_checkable
class VectorStore(Protocol):
    """向量存储端口，不绑定 pgvector、Milvus、Qdrant 或其他实现。"""

    async def upsert(
        self,
        records: Sequence[VectorRecord],
        *,
        access: AccessContext | None = None,
    ) -> list[str]: ...

    async def delete(
        self,
        record_ids: Sequence[str],
        *,
        access: AccessContext | None = None,
    ) -> int: ...

    async def search(self, query: VectorQuery) -> list[VectorSearchResult]: ...


@runtime_checkable
class Reranker(Protocol):
    """可选的二阶段重排端口。"""

    async def rerank(
        self,
        query: RetrievalQuery,
        results: Sequence[RetrievalResult],
    ) -> list[RetrievalResult]: ...
