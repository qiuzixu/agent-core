"""Rerank 二阶段重排的单元测试：精排生效、降级回退与超额召回截断。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agent_core.access import AccessContext
from agent_core.documents import Document
from agent_core.retrieval.memory import EmbeddingRetriever, InMemoryVectorStore
from agent_core.retrieval.rerank import RerankClient, RerankingRetriever
from agent_core.retrieval.types import RetrievalQuery, RetrievalResult, SearchType


def _result(text: str, score: float) -> RetrievalResult:
    return RetrievalResult(
        document=Document(content=text),
        score=score,
        rank=0,
    )


def _rerank_handler(order: list[int], scores: list[float]) -> Any:
    """构造返回固定排序的 /rerank 假端点。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b"query" in body
        results = [
            {"index": index, "relevance_score": score}
            for index, score in zip(order, scores, strict=True)
        ]
        return httpx.Response(200, json={"results": results})

    return handler


class _FakeReranker:
    """测试用假 Reranker：按内容关键字重排。"""

    async def rerank(self, query: RetrievalQuery, results: Any) -> list[RetrievalResult]:
        keyword = query.text.strip().lower()
        matched = [item for item in results if keyword in item.document.content]
        others = [item for item in results if keyword not in item.document.content]
        return matched + others


class _FakeRetriever:
    """测试用假 Retriever：返回固定候选序列。"""

    def __init__(self, candidates: list[RetrievalResult]) -> None:
        self.candidates = candidates
        self.last_limit: int | None = None

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        self.last_limit = query.limit
        return self.candidates[: query.limit]


async def test_rerank_client_reorders_and_rewrites_rank() -> None:
    results = [_result("alpha", 0.1), _result("beta", 0.9), _result("gamma", 0.5)]
    transport = httpx.MockTransport(_rerank_handler(order=[1, 2, 0], scores=[0.95, 0.6, 0.2]))
    client = RerankClient(
        api_key="test",
        model="rerank-test",
        http_client=httpx.AsyncClient(transport=transport),
    )
    query = RetrievalQuery(text="低空 禁飞", limit=3)
    reranked = await client.rerank(query, results)

    assert [item.document.content for item in reranked] == ["beta", "gamma", "alpha"]
    assert [item.rank for item in reranked] == [1, 2, 3]
    assert reranked[0].score == pytest.approx(0.95)


async def test_rerank_client_falls_back_on_error() -> None:
    results = [_result("alpha", 0.1), _result("beta", 0.9)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    client = RerankClient(
        api_key="test",
        model="rerank-test",
        http_client=httpx.AsyncClient(transport=transport),
    )
    query = RetrievalQuery(text="q", limit=2)
    reranked = await client.rerank(query, results)
    # 服务故障时回退原始排序，主链路不被阻断。
    assert [item.document.content for item in reranked] == ["alpha", "beta"]


async def test_rerank_client_respects_limit() -> None:
    results = [_result(f"doc-{i}", 0.1 * i) for i in range(5)]
    transport = httpx.MockTransport(_rerank_handler(order=[4, 3, 2, 1, 0], scores=[0.9, 0.8, 0.7, 0.6, 0.5]))
    client = RerankClient(
        api_key="test",
        model="rerank-test",
        http_client=httpx.AsyncClient(transport=transport),
    )
    query = RetrievalQuery(text="q", limit=2)
    reranked = await client.rerank(query, results)
    assert len(reranked) == 2


async def test_reranking_retriever_over_recalls_then_truncates() -> None:
    candidates = [
        _result("背景知识 alpha", 0.9),
        _result("禁飞区 规定", 0.8),
        _result("普通内容", 0.7),
        _result("禁飞 相关", 0.6),
        _result("禁飞 补充", 0.5),
        _result("其他", 0.4),
    ]
    inner = _FakeRetriever(candidates)
    retriever = RerankingRetriever(inner, _FakeReranker(), recall_multiplier=3)
    query = RetrievalQuery(text="禁飞", limit=2)
    results = await retriever.retrieve(query)

    # 第一阶段超额召回 6 条，精排后截断为 2 条、命中关键字的排前。
    assert inner.last_limit == 6
    assert len(results) == 2
    assert "禁飞" in results[0].document.content
    assert [item.rank for item in results] == [1, 2]


async def test_reranking_retriever_single_candidate_short_circuit() -> None:
    inner = _FakeRetriever([_result("only", 1.0)])
    retriever = RerankingRetriever(inner, _FakeReranker())
    results = await retriever.retrieve(RetrievalQuery(text="q", limit=3))
    assert len(results) == 1


async def test_reranking_retriever_wraps_embedding_retriever() -> None:
    """组合 EmbeddingRetriever 时接口保持兼容（add_documents / retrieve）。"""
    store = InMemoryVectorStore()

    class _StaticEmbeddings:
        async def embed_documents(self, texts: Any) -> list[list[float]]:
            return [[0.0, 1.0] for _ in texts]

        async def embed_query(self, text: str) -> list[float]:
            return [0.0, 1.0]

    inner = EmbeddingRetriever(_StaticEmbeddings(), store)
    retriever = RerankingRetriever(inner, _FakeReranker())
    docs = [Document(content="低空 禁飞 政策"), Document(content="航线 规划 指南")]
    saved = await retriever.add_documents(docs, namespace="kb")
    assert len(saved) == 2
    query = RetrievalQuery(text="禁飞", limit=2, namespace="kb", search_type=SearchType.SIMILARITY)
    results = await retriever.retrieve(query)
    assert results


