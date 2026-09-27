"""Embedding Provider 扩展契约测试。"""

from __future__ import annotations

import asyncio
import math
import unittest
from types import SimpleNamespace
from typing import Any

from agent_core import Embeddings

from agent_core_embeddings import (
    BaseProviderEmbeddings,
    EmbeddingConfigurationError,
    EmbeddingExecutionPolicy,
    EmbeddingProviderRegistry,
    EmbeddingResponseError,
    GeminiEmbeddings,
    OllamaEmbeddings,
    OpenAIEmbeddings,
    UnknownEmbeddingProviderError,
    create_embeddings,
)
from agent_core_embeddings.base import EmbeddingTask


class _OpenAIEndpoint:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        data = [
            SimpleNamespace(index=index, embedding=[float(len(text)), float(index), 1.0])
            for index, text in enumerate(kwargs["input"])
        ]
        return SimpleNamespace(data=list(reversed(data)))


class _OpenAIClient:
    def __init__(self) -> None:
        self.embeddings = _OpenAIEndpoint()


class _GeminiModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def embed_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            embeddings=[
                SimpleNamespace(values=[float(len(text)), 1.0, 0.0])
                for text in kwargs["contents"]
            ]
        )


class _GeminiClient:
    def __init__(self) -> None:
        self.models = _GeminiModels()
        self.aio = SimpleNamespace(models=self.models)


class _HttpResponse:
    def __init__(self, payload: object, *, status_code: int = 200, text: str = "") -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> object:
        return self.payload


class _HttpClient:
    def __init__(self, response: _HttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _HttpResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


class _FlakyEmbeddings(BaseProviderEmbeddings):
    provider_name = "flaky"

    def __init__(self, failures: int, vectors: list[list[float]], **kwargs: Any) -> None:
        super().__init__("fake", **kwargs)
        self.failures = failures
        self.vectors = vectors
        self.calls = 0

    async def _embed_batch(self, texts: list[str], *, task: EmbeddingTask) -> list[list[float]]:
        del texts, task
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("temporary")
        return self.vectors


class _SlowEmbeddings(BaseProviderEmbeddings):
    provider_name = "slow"

    async def _embed_batch(self, texts: list[str], *, task: EmbeddingTask) -> list[list[float]]:
        del texts, task
        await asyncio.sleep(0.1)
        return [[1.0]]


class ProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_batches_and_restores_response_order(self) -> None:
        client = _OpenAIClient()
        embeddings = OpenAIEmbeddings(
            client=client,
            dimensions=3,
            policy=EmbeddingExecutionPolicy(
                batch_size=2,
                max_concurrency=1,
                expected_dimensions=3,
            ),
        )

        vectors = await embeddings.embed_documents(["a", "bb", "ccc"])

        self.assertEqual(vectors, [[1.0, 0.0, 1.0], [2.0, 1.0, 1.0], [3.0, 0.0, 1.0]])
        self.assertEqual(len(client.embeddings.calls), 2)
        self.assertTrue(all(call["dimensions"] == 3 for call in client.embeddings.calls))
        self.assertIsInstance(embeddings, Embeddings)

    async def test_gemini_distinguishes_document_and_query_tasks(self) -> None:
        client = _GeminiClient()
        embeddings = GeminiEmbeddings(client=client, dimensions=3)

        documents = await embeddings.embed_documents(["文档"])
        query = await embeddings.embed_query("查询")

        self.assertEqual(len(documents[0]), 3)
        self.assertEqual(len(query), 3)
        self.assertEqual(
            client.models.calls[0]["config"]["task_type"],
            "RETRIEVAL_DOCUMENT",
        )
        self.assertEqual(
            client.models.calls[1]["config"]["task_type"],
            "RETRIEVAL_QUERY",
        )

    async def test_ollama_uses_native_embed_endpoint(self) -> None:
        client = _HttpClient(_HttpResponse({"embeddings": [[1, 0], [0, 1]]}))
        embeddings = OllamaEmbeddings(
            client=client,
            model="nomic-embed-text",
            base_url="http://ollama.local/",
            api_key="secret",
            policy=EmbeddingExecutionPolicy(expected_dimensions=2),
        )

        vectors = await embeddings.embed_documents(["one", "two"])

        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        call = client.calls[0]
        self.assertEqual(call["url"], "http://ollama.local/api/embed")
        self.assertEqual(call["json"]["input"], ["one", "two"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret")

    async def test_retry_and_observed_dimensions(self) -> None:
        embeddings = _FlakyEmbeddings(
            1,
            [[1.0, 2.0]],
            policy=EmbeddingExecutionPolicy(max_retries=1, retry_base_seconds=0),
        )

        vector = await embeddings.embed_query("query")

        self.assertEqual(vector, [1.0, 2.0])
        self.assertEqual(embeddings.calls, 2)
        self.assertEqual(embeddings.dimensions, 2)

    async def test_invalid_vector_and_timeout_are_rejected(self) -> None:
        invalid = _FlakyEmbeddings(0, [[math.nan]])
        slow = _SlowEmbeddings(
            "slow",
            policy=EmbeddingExecutionPolicy(timeout_seconds=0.01, max_retries=0),
        )

        with self.assertRaises(EmbeddingResponseError):
            await invalid.embed_query("query")
        with self.assertRaises(Exception) as caught:
            await slow.embed_query("query")
        self.assertEqual(caught.exception.__class__.__name__, "EmbeddingInvocationError")

    async def test_dimension_mismatch_is_rejected(self) -> None:
        embeddings = _FlakyEmbeddings(
            0,
            [[1.0, 2.0]],
            policy=EmbeddingExecutionPolicy(expected_dimensions=3),
        )

        with self.assertRaises(EmbeddingResponseError):
            await embeddings.embed_query("query")

        with self.assertRaises(EmbeddingConfigurationError):
            OpenAIEmbeddings(
                client=_OpenAIClient(),
                dimensions=2,
                policy=EmbeddingExecutionPolicy(expected_dimensions=3),
            )


class FactoryTests(unittest.TestCase):
    def test_default_factory_and_custom_registry(self) -> None:
        adapter = create_embeddings("OPENAI", client=_OpenAIClient())
        self.assertIsInstance(adapter, OpenAIEmbeddings)

        registry = EmbeddingProviderRegistry()
        registry.register("custom", lambda **_: _FlakyEmbeddings(0, [[1.0]]))
        custom = registry.create("custom")
        self.assertIsInstance(custom, Embeddings)

    def test_duplicate_unknown_and_invalid_policy_are_explicit(self) -> None:
        registry = EmbeddingProviderRegistry()
        registry.register("custom", lambda **_: _FlakyEmbeddings(0, [[1.0]]))
        with self.assertRaises(EmbeddingConfigurationError):
            registry.register("custom", lambda **_: _FlakyEmbeddings(0, [[1.0]]))
        with self.assertRaises(UnknownEmbeddingProviderError):
            registry.create("missing")
        with self.assertRaises(EmbeddingConfigurationError):
            EmbeddingExecutionPolicy(batch_size=0)


if __name__ == "__main__":
    unittest.main()
