"""Ollama Embedding 适配器。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_core_embeddings.base import BaseProviderEmbeddings, EmbeddingExecutionPolicy, EmbeddingTask
from agent_core_embeddings.errors import EmbeddingInvocationError, EmbeddingResponseError


class OllamaEmbeddings(BaseProviderEmbeddings):
    """调用 Ollama 原生 ``/api/embed`` 接口。"""

    provider_name = "ollama"

    def __init__(
        self,
        *,
        model: str = "nomic-embed-text",
        base_url: str = "http://127.0.0.1:11434",
        api_key: str | None = None,
        truncate: bool = True,
        keep_alive: str | int | None = None,
        policy: EmbeddingExecutionPolicy | None = None,
        client: Any | None = None,
    ) -> None:
        super().__init__(model, policy=policy)
        self._endpoint = f"{base_url.rstrip('/')}/api/embed"
        self._api_key = api_key
        self._truncate = truncate
        self._keep_alive = keep_alive
        self._client = client

    async def _embed_batch(self, texts: list[str], *, task: EmbeddingTask) -> list[list[float]]:
        del task
        payload: dict[str, Any] = {
            "model": self.model,
            "input": texts,
            "truncate": self._truncate,
        }
        if self._keep_alive is not None:
            payload["keep_alive"] = self._keep_alive
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        if self._client is not None:
            return await self._post(self._client, payload=payload, headers=headers)

        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "OllamaEmbeddings 需要 httpx，请安装 "
                "handwritten-agent-core-embeddings[ollama]"
            ) from exc
        async with httpx.AsyncClient() as client:
            return await self._post(client, payload=payload, headers=headers)

    async def _post(
        self,
        client: Any,
        *,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> list[list[float]]:
        response = await client.post(
            self._endpoint,
            json=dict(payload),
            headers=dict(headers),
            timeout=self.policy.timeout_seconds,
        )
        status_code = int(getattr(response, "status_code", 0))
        if status_code < 200 or status_code >= 300:
            body = str(getattr(response, "text", ""))[:500]
            raise EmbeddingInvocationError(f"Ollama 返回 HTTP {status_code}：{body}")
        try:
            value = response.json()
        except Exception as exc:
            raise EmbeddingResponseError("Ollama 返回了无效 JSON") from exc
        if not isinstance(value, Mapping):
            raise EmbeddingResponseError("Ollama 响应根节点必须是对象")
        embeddings = value.get("embeddings")
        if not isinstance(embeddings, list):
            raise EmbeddingResponseError("Ollama 响应缺少 embeddings 数组")
        vectors: list[list[float]] = []
        for item in embeddings:
            if not isinstance(item, (list, tuple)):
                raise EmbeddingResponseError("Ollama embedding 必须是数组")
            vectors.append([float(number) for number in item])
        return vectors
