"""适配器 3：Gemini。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from agent_core.errors import ModelInvocationError
from agent_core.model.core import ContextUsage, StreamChunk
from agent_core.model.providers.common import logger
from agent_core.protocol.messages import Message, assistant_message


class GeminiProvider:
    """Google Gemini 模型适配器。

    使用官方 ``google-genai`` SDK，安装方式：``uv add google-genai``。
    该 SDK 只在选择 Gemini 时延迟导入，不影响现有提供商。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-2.5-flash",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,
    ) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ImportError("GeminiProvider 需要 google-genai 包，请执行：uv add google-genai") from exc

        self._genai_types = types
        self._client = genai.Client(api_key=api_key)
        self.model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._context_window_tokens = context_window_tokens
        self.provider_name = "gemini"

    # ──────────────────────────────────────────────
    # 调用 Gemini API 进行上下文使用
    # ──────────────────────────────────────────────
    async def context_usage(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ContextUsage:
        _system_prompt, contents = self._to_gemini_contents(messages)
        try:
            response = await self._client.aio.models.count_tokens(
                model=self.model,
                contents=contents,
            )
            input_tokens = getattr(response, "total_tokens", None)
            if isinstance(input_tokens, int):
                model = await self._client.aio.models.get(model=self.model)
                model_limit = getattr(model, "input_token_limit", None)
                return ContextUsage(
                    input_tokens=input_tokens,
                    context_window_tokens=(
                        self._context_window_tokens or (model_limit if isinstance(model_limit, int) else None)
                    ),
                    exact=True,
                    source="provider_tokenizer",
                )
        except Exception as exc:
            logger.debug("Gemini tokenizer 不可用：%s", exc)
        return ContextUsage(
            input_tokens=None,
            context_window_tokens=self._context_window_tokens,
            exact=False,
            source="unavailable",
        )

    # ──────────────────────────────────────────────
    # 转换为 Gemini API 格式消息
    # ──────────────────────────────────────────────
    def _to_gemini_contents(self, messages: list[Message]) -> tuple[str | None, list[Any]]:
        """把内部消息转换为 Gemini Content/Part。"""
        types = self._genai_types
        system_instruction: str | None = None
        contents: list[Any] = []

        for message in messages:
            if message.role == "system":
                system_instruction = (
                    f"{system_instruction}\n\n{message.content}" if system_instruction else message.content
                )
                continue

            if message.role == "user":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=message.content)],
                    )
                )
                continue

            if message.role == "assistant":
                parts: list[Any] = []
                if message.content:
                    parts.append(types.Part.from_text(text=message.content))
                for call in message.tool_calls:
                    parts.append(
                        types.Part.from_function_call(
                            name=str(call.get("name", "")),
                            args=dict(call.get("args") or {}),
                        )
                    )
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                continue

            if message.role == "tool":
                # Gemini 使用 function_response 表达工具结果。
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=message.name or "tool",
                                response={"result": message.content},
                            )
                        ],
                    )
                )

        return system_instruction, contents

    # ──────────────────────────────────────────────
    # 转换为 Gemini API 格式工具
    # ──────────────────────────────────────────────
    def _to_gemini_tools(self, tools: list[dict[str, Any]]) -> list[Any]:
        """把 OpenAI function schema 转换为 Gemini function declarations。"""
        types = self._genai_types
        declarations = []
        for tool in tools:
            function = tool.get("function", tool)
            declarations.append(
                types.FunctionDeclaration(
                    name=function["name"],
                    description=function.get("description", ""),
                    parameters=function.get("parameters", {"type": "object", "properties": {}}),
                )
            )
        return [types.Tool(function_declarations=declarations)] if declarations else []

    # ──────────────────────────────────────────────
    # 构造 Gemini 请求配置
    # ──────────────────────────────────────────────
    def _config(
        self,
        *,
        system_instruction: str | None,
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> Any:
        """构造 Gemini 请求配置。"""
        kwargs: dict[str, Any] = {
            "temperature": temperature if temperature is not None else self._temperature,
            "max_output_tokens": max_tokens if max_tokens is not None else self._max_tokens,
        }
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        if tools:
            kwargs["tools"] = self._to_gemini_tools(tools)
        return self._genai_types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _response_message(response: Any) -> Message:
        """从 Gemini 响应中提取文本和函数调用。"""
        content = ""
        tool_calls: list[dict[str, Any]] = []
        candidates = getattr(response, "candidates", None) or []
        parts = getattr(getattr(candidates[0], "content", None), "parts", []) if candidates else []
        for part in parts or []:
            text = getattr(part, "text", None)
            if text:
                content += text
            function_call = getattr(part, "function_call", None)
            if function_call is not None:
                tool_calls.append(
                    {
                        "id": str(getattr(function_call, "id", "") or uuid.uuid4()),
                        "type": "function",
                        "name": str(getattr(function_call, "name", "")),
                        "args": dict(getattr(function_call, "args", {}) or {}),
                    }
                )
        return assistant_message(content, tool_calls=tool_calls)

    # ──────────────────────────────────────────────
    # 调用 Gemini API 进行聊天
    # ──────────────────────────────────────────────
    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        system_instruction, contents = self._to_gemini_contents(messages)
        config = self._config(
            system_instruction=system_instruction,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except Exception as exc:
            raise ModelInvocationError(f"Gemini API call failed: {exc}") from exc
        return self._response_message(response)

    # ──────────────────────────────────────────────
    # 调用 Gemini API 进行流式聊天
    # ──────────────────────────────────────────────
    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        system_instruction, contents = self._to_gemini_contents(messages)
        config = self._config(
            system_instruction=system_instruction,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        tool_call_map: dict[str, dict[str, Any]] = {}
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=self.model, contents=contents, config=config
            )
            async for chunk in stream:
                message = self._response_message(chunk)
                if message.content:
                    yield StreamChunk(text=message.content)
                if message.tool_calls:
                    for call_index, call in enumerate(message.tool_calls):
                        # 同一轮允许多次调用同名函数，按位置而不是名称归并增量。
                        key = f"{call_index}:{call['name']}"
                        current = tool_call_map.setdefault(
                            key,
                            {
                                "id": call["id"],
                                "type": "function",
                                "name": call["name"],
                                "args": {},
                            },
                        )
                        current["args"].update(call.get("args") or {})
        except Exception as exc:
            raise ModelInvocationError(f"Gemini stream failed: {exc}") from exc

        if tool_call_map:
            yield StreamChunk(tool_calls=list(tool_call_map.values()), is_tool_call=True)
        else:
            yield StreamChunk()
