"""不需要外部模型或密钥的最小 Agent 示例。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from agent_core import (
    ContextUsage,
    Message,
    ReActAgent,
    StreamChunk,
    ToolExecutor,
    ToolRegistry,
    assistant_message,
)


class DemoModel:
    """先请求加法工具，再根据工具结果生成最终答案。"""

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        tool_result = next((message for message in reversed(messages) if message.role == "tool"), None)
        if tool_result is None:
            return assistant_message(
                "",
                tool_calls=[
                    {
                        "id": "call-add-1",
                        "name": "add",
                        "args": {"a": 20, "b": 22},
                    }
                ],
            )
        return assistant_message(f"计算结果是 {tool_result.content}。")

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        response = await self.chat(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        yield StreamChunk(
            text=response.content,
            tool_calls=response.tool_calls,
            is_tool_call=bool(response.tool_calls),
            finish_reason="tool_calls" if response.tool_calls else "stop",
        )

    async def context_usage(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
    ) -> ContextUsage:
        return ContextUsage(
            input_tokens=sum(len(message.content) for message in messages),
            context_window_tokens=4096,
            exact=False,
            source="demo",
        )


def add(a: int, b: int) -> int:
    """返回两个整数之和。"""
    return a + b


async def main() -> None:
    registry = ToolRegistry()
    registry.register(
        "add",
        add,
        "计算两个整数之和",
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
    )
    agent = ReActAgent(
        llm=DemoModel(),
        tool_executor=ToolExecutor(registry),
        system_prompt="你是一个计算助手。",
        tool_definitions=registry.build_tool_definitions(),
    )
    print(await agent.run("计算 20 + 22"))


if __name__ == "__main__":
    asyncio.run(main())
