"""Embedding 扩展错误。"""

from agent_core import AgentError


class EmbeddingAdapterError(AgentError):
    """Embedding 适配器基础错误。"""


class EmbeddingConfigurationError(EmbeddingAdapterError):
    """Embedding Provider 配置无效。"""


class EmbeddingInvocationError(EmbeddingAdapterError):
    """Embedding Provider 调用失败。"""


class EmbeddingResponseError(EmbeddingAdapterError):
    """Provider 返回的向量数量、维度或数值无效。"""


class UnknownEmbeddingProviderError(EmbeddingConfigurationError):
    """请求了尚未注册的 Embedding Provider。"""
