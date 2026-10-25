"""SemanticCache 的 lexical / embedding / hybrid 模式回归测试。"""

from __future__ import annotations

import pytest

from agent_core.middleware.base import MiddlewareAction, MiddlewareContext
from agent_core.middleware.semantic_cache import SemanticCacheConfig, SemanticCacheMiddleware
from agent_core.protocol.messages import assistant_message, user_message
from agent_core.retrieval.memory import InMemoryVectorStore


class FakeEmbeddings:
    """测试用向量器：把同类问题映射到同一向量。"""

    def __init__(self, *, fail_query: bool = False) -> None:
        self.fail_query = fail_query
        self.query_calls = 0
        self.document_calls = 0

    async def embed_documents(self, texts):
        self.document_calls += 1
        return [self._embed(text) for text in texts]

    async def embed_query(self, text):
        self.query_calls += 1
        if self.fail_query:
            raise RuntimeError("模拟 embedding 服务不可用")
        return self._embed(text)

    @staticmethod
    def _embed(text: str) -> list[float]:
        if "虹桥" in text:
            return [0.0, 1.0]
        return [1.0, 0.0]


def make_ctx(question: str, response: str | None = None) -> MiddlewareContext:
    """构造模型执行前/后的上下文。"""
    return MiddlewareContext(
        messages=[user_message(question)],
        iteration=0,
        llm_response=assistant_message(response) if response is not None else None,
    )


async def store_answer(
    question: str,
    response: str,
    *,
    mode: str,
    embeddings: FakeEmbeddings | None,
    vector_store,
) -> None:
    """先执行一次 after_model，模拟 LLM 已给出最终答案。"""
    middleware = SemanticCacheMiddleware(
        config=SemanticCacheConfig(
            enabled=True,
            mode=mode,
            threshold=0.92,
            vector_threshold=0.90,
            lexical_floor=0.10,
            embeddings=embeddings,
            vector_store=vector_store,
        )
    )
    result = await middleware.after_model(make_ctx(question, response))
    assert result.action == MiddlewareAction.CONTINUE


async def lookup(
    question: str,
    *,
    mode: str,
    embeddings: FakeEmbeddings | None,
    vector_store,
    **overrides,
):
    """执行一次 before_model，返回缓存查找结果。"""
    config = SemanticCacheConfig(
        enabled=True,
        mode=mode,
        threshold=0.92,
        vector_threshold=0.90,
        lexical_floor=0.10,
        embeddings=embeddings,
        vector_store=vector_store,
        **overrides,
    )
    middleware = SemanticCacheMiddleware(config=config)
    return await middleware.before_model(make_ctx(question))


async def test_lexical_mode_keeps_existing_behavior() -> None:
    """lexical 默认路径不依赖 embedding，行为保持兼容。"""
    middleware = SemanticCacheMiddleware(
        config=SemanticCacheConfig(
            enabled=True,
            mode="lexical",
            threshold=0.92,
            vector_threshold=0.90,
            lexical_floor=0.10,
        )
    )
    await middleware.after_model(make_ctx("上海浦东机场在哪里", "在浦东"))
    result = await middleware.before_model(make_ctx("上海浦东机场在哪里"))
    assert result.action == MiddlewareAction.STOP
    assert result.data["cache_mode"] == "lexical"
    assert result.data["cached_response"] == "在浦东"


async def test_embedding_mode_hits_rephrased_question() -> None:
    """embedding 模式允许措辞不同但向量高度相似的问题命中。"""
    embeddings = FakeEmbeddings()
    vector_store = InMemoryVectorStore()
    await store_answer(
        "上海浦东机场的位置在哪里",
        "浦东机场在浦东新区",
        mode="embedding",
        embeddings=embeddings,
        vector_store=vector_store,
    )
    result = await lookup(
        "浦东机场的坐标是多少",
        mode="embedding",
        embeddings=embeddings,
        vector_store=vector_store,
    )
    assert result.action == MiddlewareAction.STOP
    assert result.data["cache_mode"] == "embedding"
    assert result.data["cache_score"] >= 0.90
    assert result.data["cached_response"] == "浦东机场在浦东新区"
    assert embeddings.query_calls == 1


async def test_hybrid_mode_blocks_entity_conflict() -> None:
    """hybrid 模式下，编号不同的高相似问题不能误命中。"""
    embeddings = FakeEmbeddings()
    vector_store = InMemoryVectorStore()
    await store_answer(
        "订单 A123 的状态是什么",
        "A123 已发货",
        mode="hybrid",
        embeddings=embeddings,
        vector_store=vector_store,
    )
    result = await lookup(
        "订单 A124 的状态是什么",
        mode="hybrid",
        embeddings=embeddings,
        vector_store=vector_store,
    )
    assert result.action == MiddlewareAction.CONTINUE


async def test_scope_isolates_cache_entries() -> None:
    """不同 scope 之间不共享缓存，避免模型或租户上下文互相污染。"""
    embeddings = FakeEmbeddings()
    vector_store = InMemoryVectorStore()
    await store_answer(
        "浦东机场在哪里",
        "A 租户答案",
        mode="embedding",
        embeddings=embeddings,
        vector_store=vector_store,
    )
    result = await lookup(
        "浦东机场在哪里",
        mode="embedding",
        embeddings=embeddings,
        vector_store=vector_store,
        scope="tenant-b",
    )
    assert result.action == MiddlewareAction.CONTINUE


async def test_embedding_failure_degrades_to_model_call() -> None:
    """embedding 服务异常时不能影响主流程，应继续调用模型。"""
    embeddings = FakeEmbeddings(fail_query=True)
    vector_store = InMemoryVectorStore()
    await store_answer(
        "浦东机场在哪里",
        "旧答案",
        mode="embedding",
        embeddings=FakeEmbeddings(),
        vector_store=vector_store,
    )
    result = await lookup(
        "浦东机场在哪里",
        mode="embedding",
        embeddings=embeddings,
        vector_store=vector_store,
    )
    assert result.action == MiddlewareAction.CONTINUE


def test_embedding_mode_requires_dependencies() -> None:
    """配置为 embedding/hybrid 时必须在构造期提供完整依赖。"""
    with pytest.raises(ValueError, match="embeddings 和 vector_store"):
        SemanticCacheConfig(enabled=True, mode="embedding")