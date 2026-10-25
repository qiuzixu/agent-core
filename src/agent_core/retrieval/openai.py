"""OpenAI 兼容 Embedding 适配器。

OpenAI、DashScope、Ollama 等提供 ``/embeddings`` 兼容接口的服务都可以复用；
Agent Core 本身仍不在模块导入期依赖 ``openai`` SDK。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agent_core.errors import ModelInvocationError

__all__ = ["OpenAIEmbeddings"]


class OpenAIEmbeddings:
    """调用 OpenAI 兼容 ``/embeddings`` 接口的 Embeddings 实现。"""

    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        dimensions: int | None = None,
        timeout: float | None = None,
    ) -> None:
        """构造客户端；只有真正实例化时才导入 OpenAI SDK。"""
        if not model.strip():
            raise ValueError("embedding model 不能为空")
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ModelInvocationError(
                "OpenAIEmbeddings 需要 openai 依赖，请安装 zero-agent-core[openai]"
            ) from exc
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._model = model
        self._dimensions = dimensions

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """批量把文本转成向量。"""
        values = [text for text in texts if text.strip()]
        if len(values) != len(texts):
            raise ValueError("Embedding 输入不能包含空文本")
        if not values:
            return []
        response = await self._create(values)
        return self._to_vectors(response, expected=len(values))

    async def embed_query(self, text: str) -> list[float]:
        """把单条查询转成向量。"""
        vectors = await self.embed_documents([text])
        return vectors[0]

    async def _create(self, inputs: list[str]) -> Any:
        try:
            kwargs: dict[str, Any] = {"input": inputs, "model": self._model}
            if self._dimensions is not None:
                kwargs["dimensions"] = self._dimensions
            return await self._client.embeddings.create(**kwargs)
        except Exception as exc:
            raise ModelInvocationError(f"Embedding API call failed: {exc}") from exc

    @staticmethod
    def _to_vectors(response: Any, *, expected: int) -> list[list[float]]:
        """把 SDK 返回对象转换为普通向量列表。"""
        raw_items = getattr(response, "data", None)
        if raw_items is None and isinstance(response, dict):
            raw_items = response.get("data")
        if not isinstance(raw_items, list) or len(raw_items) != expected:
            raise ModelInvocationError("Embedding API 返回数量与输入数量不一致")
        items = sorted(
            raw_items,
            key=lambda item: int(item.get("index", 0) if isinstance(item, dict) else item.index),
        )
        vectors: list[list[float]] = []
        for item in items:
            raw_vector = item.get("embedding", []) if isinstance(item, dict) else item.embedding
            vector = [float(value) for value in raw_vector]
            if not vector:
                raise ModelInvocationError("Embedding API 返回了空向量")
            vectors.append(vector)
        return vectors