"""模型调用协议。

Agent Core 只依赖该协议，不依赖 OpenAI、Anthropic、Gemini 或其他模型 SDK。
具体模型适配器由应用项目或独立 adapters 包提供。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from agent_core.protocol.messages import Message


@dataclass
class StreamChunk:
    """模型流式输出的统一数据块。"""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    is_tool_call: bool = False
    finish_reason: str | None = None


@runtime_checkable
class ModelAdapter(Protocol):
    """Agent Loop 使用的最小模型协议。"""

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message: ...

    def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]: ...


def build_tool_definition(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """构造通用的 function-calling 工具定义。"""

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
