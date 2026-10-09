"""适配器 2：Anthropic Claude。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from agent_core.errors import ModelInvocationError
from agent_core.model.core import ContextUsage, StreamChunk
from agent_core.model.providers.common import known_context_window, logger
from agent_core.protocol.messages import Message, assistant_message


class AnthropicProvider:
    """Anthropic Claude 提供商。

    需要安装：uv add anthropic
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-3-5-sonnet-20241022",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,
    ) -> None:
        try:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=api_key)
        except ImportError as exc:
            raise ImportError("AnthropicProvider 需要 anthropic 包，请执行：uv add anthropic") from exc

        self.model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._context_window_tokens = context_window_tokens
        self.provider_name = "anthropic"

    # ──────────────────────────────────────────────
    # 上下文使用
    # ──────────────────────────────────────────────
    async def context_usage(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ContextUsage:
        system_prompt, anthropic_msgs = self._to_anthropic_messages(messages)
        try:
            count_tokens = self._client.messages.count_tokens
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": anthropic_msgs,
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            if tools:
                kwargs["tools"] = self._to_anthropic_tools(tools)
            response = await count_tokens(**kwargs)
            input_tokens = getattr(response, "input_tokens", None)
            if isinstance(input_tokens, int):
                return ContextUsage(
                    input_tokens=input_tokens,
                    context_window_tokens=(self._context_window_tokens or known_context_window(self.model)),
                    exact=True,
                    source="provider_tokenizer",
                )
        except Exception as exc:
            logger.debug("Anthropic tokenizer 不可用：%s", exc)
        return ContextUsage(
            input_tokens=None,
            context_window_tokens=self._context_window_tokens or known_context_window(self.model),
            exact=False,
            source="unavailable",
        )

    # ──────────────────────────────────────────────
    # 转换为 Anthropic API 格式
    # ──────────────────────────────────────────────
    def _to_anthropic_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """将 Message 列表转为 Anthropic API 格式。

        Returns:
            (system_prompt, messages_list)
        """
        system_parts: list[str] = []
        anthropic_msgs: list[dict[str, Any]] = []

        def append_user_content(content: str | list[dict[str, Any]]) -> None:
            """合并连续 user 消息，满足 Anthropic 的消息交替约束。"""
            if anthropic_msgs and anthropic_msgs[-1]["role"] == "user":
                existing = anthropic_msgs[-1]["content"]
                if isinstance(existing, str):
                    existing = [{"type": "text", "text": existing}]
                    anthropic_msgs[-1]["content"] = existing
                if isinstance(content, str):
                    existing.append({"type": "text", "text": content})
                else:
                    existing.extend(content)
                return
            anthropic_msgs.append({"role": "user", "content": content})

        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append(msg.content)
            elif msg.role == "user":
                append_user_content(msg.content)
            elif msg.role == "assistant":
                content: list[dict[str, Any]] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        content.append(
                            {
                                "type": "tool_use",
                                "id": tc["id"],
                                "name": tc["name"],
                                "input": tc.get("args", {}),
                            }
                        )
                if content:
                    anthropic_msgs.append({"role": "assistant", "content": content})
            elif msg.role == "tool":
                append_user_content(
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id,
                            "content": msg.content,
                        }
                    ]
                )

        return "\n\n".join(system_parts) or None, anthropic_msgs

    # ──────────────────────────────────────────────
    # 转换为 Anthropic API 格式工具定义
    # ──────────────────────────────────────────────
    def _to_anthropic_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 OpenAI 格式工具定义转为 Anthropic 格式。"""
        result = []
        for t in tools:
            fn = t.get("function", t)
            result.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )
        return result

    # ──────────────────────────────────────────────
    # 调用 Anthropic API 进行 chat completion
    # ──────────────────────────────────────────────
    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        system_prompt, anthropic_msgs = self._to_anthropic_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": temperature if temperature is not None else self._temperature,
            "messages": anthropic_msgs,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

        try:
            response = await self._client.messages.create(**kwargs)
        except Exception as exc:
            raise ModelInvocationError(f"Anthropic API call failed: {exc}") from exc

        # 解析响应
        content_text = ""
        tool_calls: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                content_text += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "name": block.name,
                        "args": block.input,
                    }
                )

        logger.debug(
            "Anthropic response: content_len=%d, tool_calls=%d, stop=%s",
            len(content_text),
            len(tool_calls),
            response.stop_reason,
        )
        return assistant_message(content_text, tool_calls=tool_calls)

    # ──────────────────────────────────────────────
    # 调用 Anthropic API 进行流式 chat completion
    # ──────────────────────────────────────────────
    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        system_prompt, anthropic_msgs = self._to_anthropic_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": temperature if temperature is not None else self._temperature,
            "messages": anthropic_msgs,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

        tool_call_accum: dict[str, dict[str, Any]] = {}

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    # 不依赖 SDK 私有事件类名，兼容 anthropic 不同版本的事件对象。
                    if hasattr(event, "content_block"):
                        block = event.content_block
                        if getattr(block, "type", None) == "tool_use":
                            block_id = str(getattr(event, "index", len(tool_call_accum)))
                            tool_call_accum[block_id] = {
                                "id": getattr(block, "id", ""),
                                "name": getattr(block, "name", ""),
                                "args_json": "",
                            }

                    if hasattr(event, "delta"):
                        delta = event.delta
                        if getattr(delta, "text", None):
                            yield StreamChunk(text=delta.text)
                        elif getattr(delta, "partial_json", None) is not None:
                            # 工具调用参数增量
                            block_id = str(getattr(event, "index", len(tool_call_accum)))
                            if block_id not in tool_call_accum:
                                tool_call_accum[block_id] = {"args_json": ""}
                            tool_call_accum[block_id]["args_json"] += delta.partial_json

        except Exception as exc:
            raise ModelInvocationError(f"Anthropic stream failed: {exc}") from exc

        # 流结束后发送工具调用
        if tool_call_accum:
            tool_calls = []
            for acc in tool_call_accum.values():
                try:
                    args = json.loads(acc.get("args_json", "{}") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    {
                        "id": acc.get("id", ""),
                        "type": "function",
                        "name": acc.get("name", ""),
                        "args": args,
                    }
                )
            yield StreamChunk(tool_calls=tool_calls, is_tool_call=True)
        else:
            yield StreamChunk()
