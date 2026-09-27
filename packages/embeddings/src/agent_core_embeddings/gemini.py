"""Gemini Embedding 适配器。"""

from __future__ import annotations

from typing import Any

from agent_core_embeddings.base import (
    BaseProviderEmbeddings,
    EmbeddingExecutionPolicy,
    EmbeddingTask,
    policy_with_expected_dimensions,
)
from agent_core_embeddings.errors import EmbeddingConfigurationError, EmbeddingResponseError


class GeminiEmbeddings(BaseProviderEmbeddings):
    """使用 Google Gen AI SDK 生成文档和查询向量。"""

    provider_name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gemini-embedding-001",
        dimensions: int | None = None,
        policy: EmbeddingExecutionPolicy | None = None,
        client: Any | None = None,
    ) -> None:
        effective_policy = policy_with_expected_dimensions(policy, dimensions)
        super().__init__(model, policy=effective_policy)
        self._requested_dimensions = dimensions
        if client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise ImportError(
                    "GeminiEmbeddings 需要 google-genai，请安装 "
                    "handwritten-agent-core-embeddings[gemini]"
                ) from exc
            client = genai.Client(api_key=api_key)
        aio = getattr(client, "aio", None)
        models: Any = getattr(aio, "models", None)
        if models is None:
            raise EmbeddingConfigurationError("Gemini client 必须提供 aio.models")
        self._models: Any = models

    async def _embed_batch(self, texts: list[str], *, task: EmbeddingTask) -> list[list[float]]:
        config: dict[str, Any] = {
            "task_type": "RETRIEVAL_DOCUMENT" if task == "document" else "RETRIEVAL_QUERY"
        }
        if self._requested_dimensions is not None:
            config["output_dimensionality"] = self._requested_dimensions
        response = await self._models.embed_content(
            model=self.model,
            contents=texts,
            config=config,
        )
        embeddings = _read_value(response, "embeddings")
        if not isinstance(embeddings, (list, tuple)):
            raise EmbeddingResponseError("Gemini 响应缺少 embeddings 数组")
        vectors: list[list[float]] = []
        for item in embeddings:
            values = _read_value(item, "values")
            if not isinstance(values, (list, tuple)):
                raise EmbeddingResponseError("Gemini 响应缺少 values 数组")
            vectors.append([float(value) for value in values])
        return vectors
def _read_value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
