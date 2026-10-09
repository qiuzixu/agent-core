"""提供商构造器与注册：统一模型工厂入口。"""

from __future__ import annotations

from typing import Any

from agent_core.model.core import (
    ModelAdapter,
    create_model_provider,
    default_model_provider_registry,
)
from agent_core.model.providers.anthropic import AnthropicProvider
from agent_core.model.providers.common import logger
from agent_core.model.providers.gemini import GeminiProvider
from agent_core.model.providers.ollama import OllamaProvider
from agent_core.model.providers.openai_compatible import OpenAIProvider

# 模型提供商注册表和具体适配器都属于 agent-core；第三方 SDK 只在实例化对应
# 提供商时延迟导入，未使用的模型不会影响 Core 的基础导入。
MODEL_PROVIDER_REGISTRY = default_model_provider_registry


def _model_name(config: Any, model: str | None) -> str:
    """读取统一模型名称，并保留旧配置对象的访问方式。"""
    return model or str(getattr(config, "model_name", ""))


def _build_openai(config: Any, *, model: str | None = None, **_: Any) -> ModelAdapter:
    """构造 OpenAI 及其兼容接口适配器。"""
    api_key = getattr(config, "openai_api_key", None)
    if not api_key:
        raise ValueError("使用 OpenAI 兼容接口需要配置 AGENT_OPENAI_API_KEY")
    model_name = _model_name(config, model)
    logger.info("LLM Provider: OpenAI-compatible (%s)", model_name)
    return OpenAIProvider(
        api_key=api_key,
        base_url=getattr(config, "openai_base_url", "https://api.openai.com/v1"),
        model=model_name,
        temperature=getattr(config, "temperature", 0.0),
        max_tokens=getattr(config, "max_tokens", 4096),
        context_window_tokens=getattr(config, "context_window_tokens", None),
    )


def _build_anthropic(config: Any, *, model: str | None = None, **_: Any) -> ModelAdapter:
    """构造 Anthropic 适配器。"""
    api_key = getattr(config, "anthropic_api_key", None)
    if not api_key:
        raise ValueError("使用 Anthropic 需要配置 AGENT_ANTHROPIC_API_KEY")
    model_name = _model_name(config, model)
    logger.info("LLM Provider: Anthropic (%s)", model_name)
    return AnthropicProvider(
        api_key=api_key,
        model=model_name,
        temperature=getattr(config, "temperature", 0.0),
        max_tokens=getattr(config, "max_tokens", 4096),
        context_window_tokens=getattr(config, "context_window_tokens", None),
    )


def _build_ollama(config: Any, *, model: str | None = None, **_: Any) -> ModelAdapter:
    """构造 Ollama 本地模型适配器。"""
    model_name = _model_name(config, model)
    base_url = getattr(config, "ollama_base_url", "http://localhost:11434/v1")
    logger.info("LLM Provider: Ollama (%s @ %s)", model_name, base_url)
    return OllamaProvider(
        model=model_name,
        base_url=base_url,
        temperature=getattr(config, "temperature", 0.0),
        max_tokens=getattr(config, "max_tokens", 4096),
        context_window_tokens=getattr(config, "context_window_tokens", None),
    )


def _build_gemini(config: Any, *, model: str | None = None, **_: Any) -> ModelAdapter:
    """构造 Google Gemini 适配器。"""
    api_key = getattr(config, "gemini_api_key", None)
    if not api_key:
        raise ValueError("使用 Gemini 需要配置 AGENT_GEMINI_API_KEY")
    model_name = _model_name(config, model)
    logger.info("LLM Provider: Gemini (%s)", model_name)
    return GeminiProvider(
        api_key=api_key,
        model=model_name,
        temperature=getattr(config, "temperature", 0.0),
        max_tokens=getattr(config, "max_tokens", 4096),
        context_window_tokens=getattr(config, "context_window_tokens", None),
    )


MODEL_PROVIDER_REGISTRY.register("openai", _build_openai)
MODEL_PROVIDER_REGISTRY.register("anthropic", _build_anthropic)
MODEL_PROVIDER_REGISTRY.register("ollama", _build_ollama)
MODEL_PROVIDER_REGISTRY.register("gemini", _build_gemini)


def create_llm_provider(config: Any, *, model: str | None = None) -> ModelAdapter:
    """兼容旧调用方的模型工厂入口。

    选择、注册和具体 SDK 适配器都由 ``agent_core`` 提供。
    旧代码仍可继续调用 ``create_llm_provider(config, model=...)``。
    """
    return create_model_provider(
        config,
        provider=getattr(config, "llm_provider", "openai"),
        model=model,
    )
