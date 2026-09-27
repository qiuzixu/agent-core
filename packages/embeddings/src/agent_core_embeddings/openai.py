"""OpenAI Embedding 适配器。"""

from __future__ import annotations

from typing import Any

from agent_core_embeddings.base import (
    BaseProviderEmbeddings,
    EmbeddingExecutionPolicy,
    EmbeddingTask,
    policy_with_expected_dimensions,
)
from agent_core_embeddings.errors import EmbeddingResponseError


class OpenAIEmbeddings(BaseProviderEmbeddings):
    """使用 OpenAI Embeddings API 生成文档和查询向量。"""

    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        base_url: str | None = None,
        dimensions: int | None = None,
        policy: EmbeddingExecutionPolicy | None = None,
        client: Any | None = None,
    ) -> None:
        effective_policy = policy_with_expected_dimensions(policy, dimensions)
        super().__init__(model, policy=effective_policy)
        self._requested_dimensions = dimensions
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise ImportError(
                    "OpenAIEmbeddings 需要 openai，请安装 "
                    "handwritten-agent-core-embeddings[openai]"
                ) from exc
            kwargs: dict[str, Any] = {}
            if api_key is not None:
                kwargs["api_key"] = api_key
            if base_url is not None:
                kwargs["base_url"] = base_url
            client = AsyncOpenAI(**kwargs)
        self._client = client

    async def _embed_batch(self, texts: list[str], *, task: EmbeddingTask) -> list[list[float]]:
        del task
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }
        if self._requested_dimensions is not None:
            kwargs["dimensions"] = self._requested_dimensions
        response = await self._client.embeddings.create(**kwargs)
        data = _read_value(response, "data")
        if not isinstance(data, list):
            try:
                data = list(data)
            except TypeError as exc:
                raise EmbeddingResponseError("OpenAI 响应缺少 data 数组") from exc
        ordered = sorted(data, key=lambda item: int(_read_value(item, "index", 0)))
        vectors: list[list[float]] = []
        for item in ordered:
            embedding = _read_value(item, "embedding")
            if not isinstance(embedding, (list, tuple)):
                raise EmbeddingResponseError("OpenAI 响应缺少 embedding 数组")
            vectors.append([float(value) for value in embedding])
        return vectors
def _read_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
