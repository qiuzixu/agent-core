"""Embedding Provider 注册表和工厂。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_core import Embeddings

from agent_core_embeddings.errors import (
    EmbeddingConfigurationError,
    UnknownEmbeddingProviderError,
)
from agent_core_embeddings.gemini import GeminiEmbeddings
from agent_core_embeddings.ollama import OllamaEmbeddings
from agent_core_embeddings.openai import OpenAIEmbeddings

type EmbeddingProviderFactory = Callable[..., Embeddings]


def _normalize_provider(name: str) -> str:
    normalized = name.strip().lower().replace("_", "-")
    if not normalized:
        raise EmbeddingConfigurationError("Embedding Provider 名称不能为空")
    return normalized


class EmbeddingProviderRegistry:
    """保存 Provider 名称与构造函数，不动态导入配置中的类路径。"""

    def __init__(self) -> None:
        self._factories: dict[str, EmbeddingProviderFactory] = {}

    def register(
        self,
        name: str,
        factory: EmbeddingProviderFactory,
        *,
        replace: bool = False,
    ) -> None:
        normalized = _normalize_provider(name)
        if normalized in self._factories and not replace:
            raise EmbeddingConfigurationError(f"Embedding Provider 已注册：{normalized}")
        self._factories[normalized] = factory

    def create(self, name: str, **kwargs: Any) -> Embeddings:
        normalized = _normalize_provider(name)
        factory = self._factories.get(normalized)
        if factory is None:
            raise UnknownEmbeddingProviderError(
                f"未知 Embedding Provider：{name}；可用值：{', '.join(self.names())}"
            )
        return factory(**kwargs)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


default_embedding_provider_registry = EmbeddingProviderRegistry()
default_embedding_provider_registry.register("openai", OpenAIEmbeddings)
default_embedding_provider_registry.register("gemini", GeminiEmbeddings)
default_embedding_provider_registry.register("ollama", OllamaEmbeddings)


def create_embeddings(
    provider: str,
    *,
    registry: EmbeddingProviderRegistry | None = None,
    **kwargs: Any,
) -> Embeddings:
    """按注册名创建符合 Core ``Embeddings`` 协议的适配器。"""
    return (registry or default_embedding_provider_registry).create(provider, **kwargs)
