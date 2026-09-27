"""Agent Core 的 Embedding Provider 扩展。"""

from agent_core_embeddings.base import BaseProviderEmbeddings, EmbeddingExecutionPolicy
from agent_core_embeddings.errors import (
    EmbeddingAdapterError,
    EmbeddingConfigurationError,
    EmbeddingInvocationError,
    EmbeddingResponseError,
    UnknownEmbeddingProviderError,
)
from agent_core_embeddings.factory import (
    EmbeddingProviderFactory,
    EmbeddingProviderRegistry,
    create_embeddings,
    default_embedding_provider_registry,
)
from agent_core_embeddings.gemini import GeminiEmbeddings
from agent_core_embeddings.ollama import OllamaEmbeddings
from agent_core_embeddings.openai import OpenAIEmbeddings

__all__ = [
    "BaseProviderEmbeddings",
    "EmbeddingAdapterError",
    "EmbeddingConfigurationError",
    "EmbeddingExecutionPolicy",
    "EmbeddingInvocationError",
    "EmbeddingProviderFactory",
    "EmbeddingProviderRegistry",
    "EmbeddingResponseError",
    "GeminiEmbeddings",
    "OllamaEmbeddings",
    "OpenAIEmbeddings",
    "UnknownEmbeddingProviderError",
    "create_embeddings",
    "default_embedding_provider_registry",
]
