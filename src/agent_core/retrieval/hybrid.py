"""混合检索（Keyword + Semantic，RRF 融合）。

设计要点：
- 关键词路负责精确术语命中，语义路负责同义表述召回；
- 每一路独立排序后使用 Reciprocal Rank Fusion（RRF）融合，
  避免两种分数语义不同却直接相加的问题；
- 同一文档只保留一个融合结果，分数为所有出现位置贡献之和。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from agent_core.access import AccessContext
from agent_core.documents import Document
from agent_core.retrieval.core import WritableRetriever
from agent_core.retrieval.types import RetrievalQuery, RetrievalResult, SearchType

# RRF 常数；越大，头部名次带来的分差越平滑。
_RRF_K = 60


class HybridRetriever:
    """组合关键词和语义两条召回链路，并输出统一的融合排序结果。"""

    def __init__(
        self,
        keyword_retriever: WritableRetriever,
        vector_retriever: WritableRetriever,
        *,
        recall_multiplier: int = 3,
    ) -> None:
        """``recall_multiplier`` 控制每一路超额召回的候选规模。"""
        if recall_multiplier < 1:
            raise ValueError("recall_multiplier 必须大于等于 1")
        self._keyword = keyword_retriever
        self._vector = vector_retriever
        self._recall_multiplier = recall_multiplier

    async def add_documents(
        self,
        documents: Sequence[Document],
        *,
        namespace: str = "default",
        access: AccessContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        """同时写入两条链路；兼容同步与异步 add_documents。"""
        saved: list[str] = []
        # KeywordRetriever 的接口通常是同步的；Reranking/Embedding 是异步的。
        keyword_added = self._keyword.add_documents(
            documents,
            namespace=namespace,
            access=access,
            metadata=metadata,
        )
        if hasattr(keyword_added, "__await__"):
            saved.extend(await keyword_added)
        else:
            saved.extend(keyword_added)
        vector_added = self._vector.add_documents(
            documents,
            namespace=namespace,
            access=access,
            metadata=metadata,
        )
        if hasattr(vector_added, "__await__"):
            saved.extend(await vector_added)
        else:
            saved.extend(vector_added)
        return saved

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        """执行双路召回并按 RRF 排序。"""
        over_limit = query.limit * self._recall_multiplier
        keyword_query = replace(query, limit=over_limit, search_type=SearchType.KEYWORD)
        vector_query = replace(query, limit=over_limit, search_type=SearchType.SIMILARITY)
        keyword_results, vector_results = await self._retrieve_both(keyword_query, vector_query)

        # document_id -> (result, rrf_score)
        merged: dict[str, tuple[RetrievalResult, float]] = {}
        for results in (keyword_results, vector_results):
            for rank, item in enumerate(results, start=1):
                key = item.document.document_id
                contribution = 1.0 / (_RRF_K + rank)
                current = merged.get(key)
                if current is None:
                    merged[key] = (item, contribution)
                else:
                    # 保留任一路的原文档；元数据保留首路的，并记录另一路命中。
                    merged[key] = (current[0], current[1] + contribution)

        # 融合分数只用于排序；最终输出改为全局 rank，方便调用方展示。
        ordered = sorted(
            merged.values(),
            key=lambda pair: (-pair[1], pair[0].document.document_id),
        )[: query.limit]
        return [
            replace(
                item,
                score=round(score, 6),
                rank=index,
                metadata={**item.metadata, "fusion": "rrf"},
            )
            for index, (item, score) in enumerate(ordered, start=1)
        ]

    async def _retrieve_both(
        self,
        keyword_query: RetrievalQuery,
        vector_query: RetrievalQuery,
    ) -> tuple[list[RetrievalResult], list[RetrievalResult]]:
        """双路并发召回；其中一路失败时返回空列表，避免另一路也被拖垮。"""
        keyword_task = self._keyword.retrieve(keyword_query)
        vector_task = self._vector.retrieve(vector_query)
        keyword_results = await self._safe_retrieve(keyword_task)
        vector_results = await self._safe_retrieve(vector_task)
        return keyword_results, vector_results

    @staticmethod
    async def _safe_retrieve(task: Any) -> list[RetrievalResult]:
        """单路异常降级为空候选；调用方仍能获得另一路结果。"""
        try:
            return list(await task)
        except Exception:
            return []

