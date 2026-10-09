"""适配器 4：Ollama（本地模型）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agent_core.model.core import ContextUsage, StreamChunk
from agent_core.model.providers.openai_compatible import OpenAIProvider
from agent_core.protocol.messages import Message


class OllamaProvider:
    """Ollama 本地模型提供商（复用 OpenAI 兼容接口）。

    Ollama 提供 OpenAI 兼容的 API，直接复用 OpenAIProvider。

    需要先启动 Ollama：ollama serve
    支持的模型：qwen2.5, llama3.2, mistral, deepseek-r1 等
    """

    def __init__(
        self,
        *,
        model: str = "qwen2.5:7b",
        base_url: str = "http://localhost:11434/v1",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,
    ) -> None:
        self._inner = OpenAIProvider(
            api_key="ollama",  # Ollama 不需要真实 key
            base_url=base_url,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            context_window_tokens=context_window_tokens,
        )
        self.model = model
        self.provider_name = "ollama"

    # ──────────────────────────────────────────────
    # 获取上下文使用统计
    # ──────────────────────────────────────────────
    async def context_usage(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ContextUsage:
        return await self._inner.context_usage(messages, tools=tools)

    # ──────────────────────────────────────────────
    # 调用 Ollama API 进行聊天
    # ──────────────────────────────────────────────
    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        return await self._inner.chat(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)

    # ──────────────────────────────────────────────
    # 调用 Ollama API 进行流式聊天
    # ──────────────────────────────────────────────
    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        async for chunk in self._inner.stream_chat(
            messages, tools=tools, temperature=temperature, max_tokens=max_tokens
        ):
            yield chunk
