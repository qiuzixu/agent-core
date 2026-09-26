"""注册应用自有模型适配器的示例。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from agent_core import (
    ContextUsage,
    Message,
    StreamChunk,
    assistant_message,
    create_model_provider,
    register_model_provider,
)


class PrivateModel:
    """演示用私有模型；真实项目在这里调用内部 SDK。"""

    def __init__(self, endpoint: str, model: str) -> None:
        self.endpoint = endpoint
        self.model = model

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        return assistant_message(f"{self.model} 已收到 {len(messages)} 条消息")

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        response = await self.chat(messages, tools=tools)
        yield StreamChunk(text=response.content, finish_reason="stop")

    async def context_usage(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
    ) -> ContextUsage:
        return ContextUsage(None, 32_000, False, "private-model")


@dataclass
class Settings:
    llm_provider: str = "private"
    model_name: str = "private-chat"
    private_endpoint: str = "http://model.internal/v1"


def build_private_model(config: Settings, *, model: str | None = None, **_: object) -> PrivateModel:
    """把应用配置转换成统一模型适配器。"""
    return PrivateModel(config.private_endpoint, model or config.model_name)


register_model_provider("private", build_private_model)
model = create_model_provider(Settings())

print(type(model).__name__, model.endpoint, model.model)
