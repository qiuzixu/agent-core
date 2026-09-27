"""Embedding Provider 共用执行策略和校验。"""

from __future__ import annotations

import asyncio
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from agent_core_embeddings.errors import (
    EmbeddingConfigurationError,
    EmbeddingInvocationError,
    EmbeddingResponseError,
)

EmbeddingTask = Literal["document", "query"]


@dataclass(frozen=True)
class EmbeddingExecutionPolicy:
    """跨 Provider 共用的批处理和调用策略。"""

    batch_size: int = 64
    max_concurrency: int = 2
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_base_seconds: float = 0.5
    expected_dimensions: int | None = None

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise EmbeddingConfigurationError("batch_size 必须大于 0")
        if self.max_concurrency <= 0:
            raise EmbeddingConfigurationError("max_concurrency 必须大于 0")
        if self.timeout_seconds <= 0:
            raise EmbeddingConfigurationError("timeout_seconds 必须大于 0")
        if self.max_retries < 0:
            raise EmbeddingConfigurationError("max_retries 不能小于 0")
        if self.retry_base_seconds < 0:
            raise EmbeddingConfigurationError("retry_base_seconds 不能小于 0")
        if self.expected_dimensions is not None and self.expected_dimensions <= 0:
            raise EmbeddingConfigurationError("expected_dimensions 必须大于 0")


class BaseProviderEmbeddings(ABC):
    """为具体 Provider 提供批处理、重试、超时和向量校验。"""

    provider_name = "base"

    def __init__(self, model: str, *, policy: EmbeddingExecutionPolicy | None = None) -> None:
        if not model.strip():
            raise EmbeddingConfigurationError("Embedding model 不能为空")
        self.model = model
        self.policy = policy or EmbeddingExecutionPolicy()
        self._observed_dimensions: int | None = None
        self._dimension_lock = asyncio.Lock()

    @property
    def dimensions(self) -> int | None:
        """返回配置或最近一次成功响应的向量维度。"""
        return self.policy.expected_dimensions or self._observed_dimensions

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        values = [str(text) for text in texts]
        if not values:
            return []
        return await self._embed_many(values, task="document")

    async def embed_query(self, text: str) -> list[float]:
        if not text.strip():
            raise EmbeddingConfigurationError("查询文本不能为空")
        vectors = await self._embed_many([text], task="query")
        return vectors[0]

    async def _embed_many(self, texts: list[str], *, task: EmbeddingTask) -> list[list[float]]:
        batches = [
            texts[offset : offset + self.policy.batch_size]
            for offset in range(0, len(texts), self.policy.batch_size)
        ]
        semaphore = asyncio.Semaphore(self.policy.max_concurrency)

        async def execute(batch: list[str]) -> list[list[float]]:
            async with semaphore:
                return await self._execute_batch(batch, task=task)

        results = await asyncio.gather(*(execute(batch) for batch in batches))
        vectors = [vector for batch in results for vector in batch]
        if len(vectors) != len(texts):
            raise EmbeddingResponseError(
                f"{self.provider_name} 返回 {len(vectors)} 个向量，期望 {len(texts)} 个"
            )
        await self._validate_vectors(vectors)
        return vectors

    async def _execute_batch(self, texts: list[str], *, task: EmbeddingTask) -> list[list[float]]:
        last_error: Exception | None = None
        attempts = self.policy.max_retries + 1
        for attempt in range(attempts):
            try:
                vectors = await asyncio.wait_for(
                    self._embed_batch(texts, task=task),
                    timeout=self.policy.timeout_seconds,
                )
                if len(vectors) != len(texts):
                    raise EmbeddingResponseError(
                        f"{self.provider_name} 当前批次返回 {len(vectors)} 个向量，"
                        f"期望 {len(texts)} 个"
                    )
                return vectors
            except (EmbeddingConfigurationError, EmbeddingResponseError):
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                delay = self.policy.retry_base_seconds * (2**attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
        raise EmbeddingInvocationError(
            f"{self.provider_name} Embedding 调用失败，已尝试 {attempts} 次"
        ) from last_error

    async def _validate_vectors(self, vectors: list[list[float]]) -> None:
        if not vectors:
            raise EmbeddingResponseError(f"{self.provider_name} 没有返回向量")
        async with self._dimension_lock:
            expected = self.policy.expected_dimensions or self._observed_dimensions or len(vectors[0])
            if expected <= 0:
                raise EmbeddingResponseError("Embedding 向量不能为空")
            for vector in vectors:
                if len(vector) != expected:
                    raise EmbeddingResponseError(
                        f"Embedding 向量维度不一致：期望 {expected}，实际 {len(vector)}"
                    )
                if not all(math.isfinite(value) for value in vector):
                    raise EmbeddingResponseError("Embedding 向量包含 NaN 或 Infinity")
            self._observed_dimensions = expected

    @abstractmethod
    async def _embed_batch(self, texts: list[str], *, task: EmbeddingTask) -> list[list[float]]:
        """调用具体 Provider 生成一批向量。"""


def policy_with_expected_dimensions(
    policy: EmbeddingExecutionPolicy | None,
    dimensions: int | None,
) -> EmbeddingExecutionPolicy:
    """把 Provider 的维度参数合并进通用校验策略。"""
    if dimensions is None:
        return policy or EmbeddingExecutionPolicy()
    if dimensions <= 0:
        raise EmbeddingConfigurationError("dimensions 必须大于 0")
    if policy is None:
        return EmbeddingExecutionPolicy(expected_dimensions=dimensions)
    if policy.expected_dimensions not in {None, dimensions}:
        raise EmbeddingConfigurationError("dimensions 与 policy.expected_dimensions 不一致")
    return replace(policy, expected_dimensions=dimensions)
