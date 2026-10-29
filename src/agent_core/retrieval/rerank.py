"""二阶段重排（Rerank）实现：召回结果的精排客户端与包装 Retriever。

RerankClient 通过 OpenAI 兼容生态常见的 ``POST /rerank`` 接口
（SiliconFlow / Jina / Cohere / DashScope rerank 等）对第一阶段召回的候选
做 Cross-Encoder 精排；服务不可用时默认回退原始排序（rerank 是增强不是依赖）。
RerankingRetriever 包装任意 Retriever，先超额召回再精排截断。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Sequence
from typing import Any

from agent_core.access import AccessContext
from agent_core.errors import ModelInvocationError
from agent_core.retrieval.core import Retriever, Reranker
from agent_core.retrieval.types import RetrievalQuery, RetrievalResult

logger = logging.getLogger(__name__)

__all__ = ["RerankClient", "RerankingRetriever"]


class RerankClient:
    """调用 OpenAI 兼容 ``/rerank`` 接口的 Reranker 实现。

    请求体为业界通用格式 ``{"model", "query", "documents", "top_n"}``，
    返回 ``results: [{index, relevance_score}, ...]``（Cohere / Jina /
    SiliconFlow / DashScope 兼容端点均遵循该结构）。
    服务调用失败时回退为原始排序，保证检索主链路不被 rerank 故障阻断。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        timeout: float | None = None,
        fallback_on_error: bool = True,
        http_client: Any | None = None,
    ) -> None:
        """构造客户端；``http_client`` 主要供测试注入 httpx.MockTransport。"""
        if not model.strip():
            raise ValueError("rerank model 不能为空")
        try:
            import httpx
        except ImportError as exc:
            raise ModelInvocationError(
                "RerankClient 需要 httpx 依赖，请安装 zero-agent-core[openai]"
            ) from exc
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._fallback_on_error = fallback_on_error
        self._client = http_client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    async def rerank(
        self,
        query: RetrievalQuery,
        results: Sequence[RetrievalResult],
    ) -> list[RetrievalResult]:
        """按相关性重排候选；``top_n`` 以查询 limit 为准，失败时回退原序。"""
        if not results:
            return []
        documents = [item.document.content for item in results]
        try:
            ordering = await self._request_scores(query.text, documents, top_n=query.limit)
        except Exception as exc:
            if not self._fallback_on_error:
                raise ModelInvocationError(f"Rerank API 调用失败: {exc}") from exc
            logger.warning("[Rerank] 调用失败，回退原始排序: %s", exc)
            return list(results)[: query.limit]

        reranked: list[RetrievalResult] = []
        for rank, (index, score) in enumerate(ordering, start=1):
            if len(reranked) >= query.limit:
                break
            if index < 0 or index >= len(results):
                continue
            original = results[index]
            reranked.append(
                RetrievalResult(
                    document=original.document,
                    score=score,
                    rank=rank,
                    metadata=dict(original.metadata),
                )
            )
        # API 可能只返回部分结果，剩余候选按原顺序追加，保证不丢条目。
        seen = {index for index, _ in ordering}
        for position, original in enumerate(results):
            if position in seen or len(reranked) >= query.limit:
                break
            reranked.append(
                RetrievalResult(
                    document=original.document,
                    score=original.score,
                    rank=len(reranked) + 1,
                    metadata=dict(original.metadata),
                )
            )
        return reranked

    async def _request_scores(
        self,
        query_text: str,
        documents: Sequence[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]:
        """调用远端 rerank 接口，返回 [(原始候选下标, 相关性得分)] 按分数降序。"""
        response = await self._client.post(
            f"{self._base_url}/rerank",
            json={
                "model": self._model,
                "query": query_text,
                "documents": list(documents),
                "top_n": top_n,
            },
        )
        response.raise_for_status()
        body = response.json()
        raw_results = body.get("results")
        if not isinstance(raw_results, list):
            raise ModelInvocationError("Rerank API 返回缺少 results 字段")
        ordering: list[tuple[int, float]] = []
        for item in raw_results:
            index = int(item.get("index", -1))
            score = float(item.get("relevance_score", 0.0))
            ordering.append((index, score))
        ordering.sort(key=lambda pair: -pair[1])
        return ordering


class RerankingRetriever:
    """二阶段 Retriever：先超额召回，再精排截断到目标条数。

    ``recall_multiplier`` 控制超额召回倍数（默认 3 倍）：第一阶段取
    ``limit * recall_multiplier`` 条候选，交给 Reranker 精排后只保留
    top-limit 条，用小成本显著提升进入 LLM 上下文的文档质量。
    """

    def __init__(
        self,
        inner: Retriever,
        reranker: Reranker,
        *,
        recall_multiplier: int = 3,
    ) -> None:
        if recall_multiplier < 1:
            raise ValueError("recall_multiplier 必须大于等于 1")
        self._inner = inner
        self._reranker = reranker
        self._recall_multiplier = recall_multiplier

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        from dataclasses import replace

        recall_query = replace(query, limit=query.limit * self._recall_multiplier)
        candidates = await self._inner.retrieve(recall_query)
        if len(candidates) <= 1:
            return candidates
        reranked = await self._reranker.rerank(query, candidates)
        return [
            RetrievalResult(
                document=item.document,
                score=item.score,
                rank=rank,
                metadata=dict(item.metadata),
            )
            for rank, item in enumerate(reranked[: query.limit], start=1)
        ]

    async def add_documents(
        self,
        documents: Sequence[Any],
        *,
        namespace: str = "default",
        access: AccessContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        """透传写入接口；按内部 Retriever 实际签名适配参数。"""
        add = getattr(self._inner, "add_documents", None)
        if add is None:
            raise AttributeError("内部 Retriever 不支持 add_documents")
        kwargs: dict[str, Any] = {}
        params = inspect.signature(add).parameters
        if "namespace" in params:
            kwargs["namespace"] = namespace
        if "access" in params:
            kwargs["access"] = access
        if "metadata" in params:
            kwargs["metadata"] = metadata
        result = add(documents, **kwargs)
        if inspect.iscoroutine(result):
            return await result
        return result
