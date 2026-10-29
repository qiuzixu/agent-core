"""Hybrid 检索的单元测试：RRF 排序、去重合并与单路降级。"""

from __future__ import annotations

from agent_core.documents import Document
from agent_core.retrieval.hybrid import HybridRetriever
from agent_core.retrieval.memory import EmbeddingRetriever, InMemoryVectorStore, KeywordRetriever
from agent_core.retrieval.types import RetrievalQuery, RetrievalResult, SearchType


class _FakeRetriever:
    """测试用假 Retriever：返回固定候选序列。"""

    def __init__(self, candidates: list[RetrievalResult]) -> None:
        self.candidates = candidates
        self.last_limit: int | None = None

    def add_documents(self, documents, **kwargs):
        return [document.document_id for document in documents]

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        self.last_limit = query.limit
        return self.candidates[: query.limit]


class _ExplodingRetriever:
    """用于验证另一路失败的降级行为。"""

    def add_documents(self, documents, **kwargs):
        return [document.document_id for document in documents]

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        raise RuntimeError("downstream unavailable")


async def test_hybrid_rrf_prefers_document_hit_by_both_paths() -> None:
    """同时被双路命中的文档应排到单路命中之前。"""
    doc_both = Document(content="both", document_id="both")
    doc_keyword = Document(content="keyword", document_id="keyword")
    doc_vector = Document(content="vector", document_id="vector")
    keyword = _FakeRetriever([
        RetrievalResult(document=doc_keyword, score=0.9, rank=1),
        RetrievalResult(document=doc_both, score=0.8, rank=2),
    ])
    vector = _FakeRetriever([
        RetrievalResult(document=doc_both, score=0.9, rank=1),
        RetrievalResult(document=doc_vector, score=0.8, rank=2),
    ])
    retriever = HybridRetriever(keyword, vector)
    results = await retriever.retrieve(
        RetrievalQuery(text="q", limit=3, search_type=SearchType.HYBRID)
    )

    assert [item.document.document_id for item in results] == ["both", "keyword", "vector"]
    assert [item.rank for item in results] == [1, 2, 3]
    assert all(item.metadata["fusion"] == "rrf" for item in results)
    assert keyword.last_limit == 9
    assert vector.last_limit == 9


async def test_hybrid_deduplicates_by_document_id() -> None:
    """同一文档双路命中只输出一次。"""
    doc = Document(content="same", document_id="same")
    keyword = _FakeRetriever([RetrievalResult(document=doc, score=1.0, rank=1)])
    vector = _FakeRetriever([RetrievalResult(document=doc, score=1.0, rank=1)])
    retriever = HybridRetriever(keyword, vector)
    results = await retriever.retrieve(RetrievalQuery(text="q", limit=5))
    assert len(results) == 1
    assert results[0].score > 0.01


async def test_hybrid_degrades_when_one_path_fails() -> None:
    """一路异常时另一路仍可返回结果。"""
    doc = Document(content="keyword", document_id="keyword")
    keyword = _FakeRetriever([RetrievalResult(document=doc, score=1.0, rank=1)])
    retriever = HybridRetriever(keyword, _ExplodingRetriever())
    results = await retriever.retrieve(RetrievalQuery(text="q", limit=3))
    assert [item.document.document_id for item in results] == ["keyword"]


async def test_hybrid_add_documents_supports_keyword_and_embedding() -> None:
    """组合真实 Keyword/Embedding Retriever 时写入接口兼容。"""

    class _StaticEmbeddings:
        async def embed_documents(self, texts):
            return [[float(len(text)), 1.0] for text in texts]

        async def embed_query(self, text):
            return [float(len(text)), 1.0]

    keyword = KeywordRetriever()
    embedding = EmbeddingRetriever(_StaticEmbeddings(), InMemoryVectorStore())
    retriever = HybridRetriever(keyword, embedding)
    doc = Document(content="低空 禁飞 政策")
    saved = await retriever.add_documents([doc], namespace="kb")
    assert saved == [doc.document_id, doc.document_id]
    results = await retriever.retrieve(
        RetrievalQuery(text="禁飞", limit=3, namespace="kb", search_type=SearchType.HYBRID)
    )
    assert len(results) == 1

