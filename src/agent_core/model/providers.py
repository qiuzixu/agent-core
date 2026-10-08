"""多模型切换：Protocol 抽象 + 各提供商适配器。

使用方式：
    # 开发环境（OpenAI 兼容接口）
    llm = OpenAIProvider(api_key="sk-...", model="gpt-4o-mini")

    # Anthropic Claude
    llm = AnthropicProvider(api_key="sk-ant-...", model="claude-3-5-sonnet-20241022")

    # 本地 Ollama
    llm = OllamaProvider(model="qwen2.5:7b")

    # 从环境变量/配置自动选择
    llm = create_llm_provider(config)

所有提供商均实现 ``ModelAdapter`` 协议，ReActAgent 只依赖该协议，
切换提供商无需修改任何 Agent 代码。
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from agent_core.errors import ModelInvocationError
from agent_core.model.core import (
    ContextUsage,
    ModelAdapter,
    StreamChunk,
    create_model_provider,
    default_model_provider_registry,
)
from agent_core.protocol.messages import Message, assistant_message

logger = logging.getLogger(__name__)

# 模型提供商注册表和具体适配器都属于 agent-core；第三方 SDK 只在实例化对应
# 提供商时延迟导入，未使用的模型不会影响 Core 的基础导入。
MODEL_PROVIDER_REGISTRY = default_model_provider_registry


# ──────────────────────────────────────────────
# 适配器实现与工厂
# ──────────────────────────────────────────────


# ── token / 上下文窗口辅助函数 ────────────────────────────────
def _read_token_count(value: Any) -> int | None:
    candidates = [value]
    if isinstance(value, dict):
        output = value.get("output")
        candidates.extend([value.get("usage"), value.get("data"), output])
        if isinstance(output, dict):
            candidates.append(output.get("usage"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("prompt_tokens", "input_tokens", "total_tokens", "token_count"):
            token_count = candidate.get(key)
            if isinstance(token_count, int) and token_count >= 0:
                return token_count
        token_ids = candidate.get("token_ids")
        if isinstance(token_ids, list):
            return len(token_ids)
    return None


# ── token / 统计完整模型输入辅助函数 ────────────────────────────────
def _count_qwen_tokens(
    messages: list[Message],
    tools: list[dict[str, Any]] | None,
) -> int | None:
    """使用 DashScope SDK 随附的官方 Qwen tokenizer 统计完整模型输入。"""
    try:
        # 导入 DashScope tokenizer
        from dashscope.tokenizers import get_tokenizer

        # 获取 Qwen tokenizer
        tokenizer = get_tokenizer("qwen-plus")
        parts: list[str] = []
        for message in messages:
            # 构建消息内容
            parts.extend(["<|im_start|>", message.role, "\n", message.content])
            # 处理工具调用
            if message.tool_calls:
                parts.append(json.dumps(message.tool_calls, ensure_ascii=False, separators=(",", ":")))
            if message.name:
                parts.extend(["\nname:", message.name])
            if message.tool_call_id:
                parts.extend(["\ntool_call_id:", message.tool_call_id])
            parts.append("<|im_end|>\n")
        if tools:
            # 构建工具定义
            parts.extend(
                [
                    "<|im_start|>system\n# Tools\n",
                    json.dumps(tools, ensure_ascii=False, separators=(",", ":")),
                    "<|im_end|>\n",
                ]
            )
        # 构建助手消息
        parts.append("<|im_start|>assistant\n")
        # 编码消息
        token_ids = tokenizer.encode("".join(parts))
        # 返回 token 数
        return len(token_ids)
    except Exception as exc:
        logger.debug("本地 Qwen tokenizer 不可用：%s", exc)
        return None


# ── token / 上下文窗口辅助函数 ────────────────────────────────
def _read_context_window(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    candidates = [value, value.get("data"), value.get("model")]  # 从多个字段中读取上下文窗口大小
    for candidate in candidates:  # 遍历候选字段
        if not isinstance(candidate, dict):  # 跳过非字典字段
            continue
        for key in (
            "context_window_tokens",
            "context_length",
            "max_context_length",
            "max_input_tokens",
            "input_token_limit",
        ):
            context_window = candidate.get(key)
            if isinstance(context_window, int) and context_window > 0:
                return context_window
    return None


# ── token / 已公开模型规格辅助函数 ────────────────────────────────
def known_context_window(model: str) -> int | None:
    """已公开模型规格；未知模型必须由 provider metadata 或配置提供。"""
    normalized = model.lower()
    known_windows = {
        "gpt-4o": 128_000,
        "gpt-4o-mini": 128_000,
        "gpt-4.1": 1_047_576,
        "gpt-4.1-mini": 1_047_576,
        "gpt-4.1-nano": 1_047_576,
        "qwen3.7-plus": 1_000_000,
        "qwen3-max": 262_144,
    }
    return known_windows.get(normalized)


# ──────────────────────────────────────────────
# 适配器 1：OpenAI / 兼容接口（默认）
# ──────────────────────────────────────────────
class OpenAIProvider:
    """OpenAI 兼容接口的模型适配器。

    功能：
    1. 普通 chat completion（支持 tool calling）
    2. 流式 chat（支持工具调用收集）
    3. JSON 结构化输出
    """

    provider_name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,  # 上下文窗口 token 数
    ) -> None:
        # 只在真正选择 OpenAI 提供商时加载 SDK，导入 Core 其他能力不应依赖它。
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise ModelInvocationError("OpenAI 提供商需要 openai 依赖，请先安装项目运行依赖。") from exc
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)  # 初始化 OpenAI 客户端
        self._api_key = api_key  # API 密钥
        self._base_url = base_url.rstrip("/")  # 去掉末尾的斜杠
        self._model = model  # 模型名称
        # 暴露只读语义的公开元数据，便于日志、路由和运行审计使用。
        self.model = model  # 模型名称
        self._temperature = temperature  # 温度参数
        self._max_tokens = max_tokens  # 最大 token 数
        self._configured_context_window_tokens = context_window_tokens  # 上下文窗口 token 数
        self._context_window_cache: int | None = None  # 上下文窗口 token 数缓存，用于优化调用

    # ──────────────────────────────────────────────
    # 普通对话
    # ──────────────────────────────────────────────

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        """调用 LLM 进行对话。

        Args:
            messages: 消息历史列表。
            tools: 可选的工具定义列表（OpenAI function calling 格式）。
            temperature: 覆盖默认温度。
            max_tokens: 覆盖默认最大 token 数。

        Returns:
            LLM 的回复消息（assistant 角色）。

        Raises:
            ModelInvocationError: LLM 调用失败。
        """
        openai_messages = [msg.to_openai_dict() for msg in messages]  # 转换为 OpenAI 格式的消息

        # 构建请求参数
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": openai_messages,
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
        }

        # 如果有工具定义，添加到请求参数
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            logger.debug(
                "LLM call: model=%s, messages=%d, tools=%d",
                self._model,
                len(messages),
                len(tools) if tools else 0,
            )
            # 调用 OpenAI API
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise ModelInvocationError(f"OpenAI API call failed: {exc}") from exc

        if not response.choices:  # 检查是否有有效回复
            raise ModelInvocationError("OpenAI API returned no choices.")
        # 解析响应
        choice = response.choices[0]  # 取第一个回复
        message = choice.message  # 取回复消息
        content = message.content or ""  # 取回复内容
        tool_calls: list[dict[str, Any]] = []  # 初始化工具调用列表
        # 解析工具调用
        if message.tool_calls:
            for tc in message.tool_calls:  # 遍历工具调用
                try:
                    arguments = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ModelInvocationError(
                        f"OpenAI 返回的工具 {tc.function.name!r} 参数不是有效 JSON：{exc}"
                    ) from exc
                if not isinstance(arguments, dict):
                    raise ModelInvocationError(f"OpenAI 返回的工具 {tc.function.name!r} 参数必须是 JSON 对象")
                tool_calls.append(
                    {
                        "id": tc.id,  # 工具调用 ID
                        "type": "function",  # 工具调用类型
                        # 工具名称
                        "name": tc.function.name,
                        # 工具参数
                        "args": arguments,
                    }
                )

        # 记录日志
        logger.debug(
            "LLM response: content_len=%d, tool_calls=%d, finish=%s",
            len(content),
            len(tool_calls),
            choice.finish_reason,
        )
        # 返回回复消息
        return assistant_message(content, tool_calls=tool_calls)

    # ──────────────────────────────────────────────
    # 上下文使用
    # ──────────────────────────────────────────────
    async def context_usage(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ContextUsage:
        """通过兼容接口的 Tokenize API 获取精确输入 token 数。"""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message.to_openai_dict() for message in messages],
        }
        if tools:
            payload["tools"] = tools

        input_tokens: int | None = None
        source = "unavailable"  # 初始化来源为不可用
        if self._model.lower().startswith("qwen"):  # 如果是 Qwen 模型
            input_tokens = _count_qwen_tokens(messages, tools)  # 计算 Qwen 模型的 token 数
            if input_tokens is not None:  # 如果计算成功
                source = "official_qwen_tokenizer"

        try:
            import httpx

            requests: list[tuple[str, dict[str, Any]]] = [
                (f"{self._base_url}/tokenizer", payload),
                (f"{self._base_url}/tokenize", payload),
            ]
            if "dashscope.aliyuncs.com" in self._base_url:
                native_payload = {
                    "model": self._model,
                    "input": {
                        "messages": payload["messages"],
                        **({"tools": tools} if tools else {}),
                    },
                }
                requests.insert(
                    0,
                    (
                        "https://dashscope.aliyuncs.com/api/v1/tokenizer",
                        native_payload,
                    ),
                )

            async with httpx.AsyncClient(timeout=15) as client:
                for url, request_payload in requests:
                    if input_tokens is not None:
                        break
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=request_payload,
                    )
                    if not response.is_success:
                        continue
                    input_tokens = _read_token_count(response.json())
                    if input_tokens is not None:
                        source = "provider_tokenizer"
                        break
        except Exception as exc:
            logger.debug("模型 tokenizer 不可用：%s", exc)

        context_window_tokens = await self._context_window_tokens()
        return ContextUsage(
            input_tokens=input_tokens,
            context_window_tokens=context_window_tokens,
            exact=input_tokens is not None,
            source=source,
        )

    # ──────────────────────────────────────────────
    # 读取模型上下文上限
    # ──────────────────────────────────────────────
    async def _context_window_tokens(self) -> int | None:
        if self._configured_context_window_tokens:
            return self._configured_context_window_tokens
        if self._context_window_cache is not None:
            return self._context_window_cache

        try:
            import httpx

            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(
                    f"{self._base_url}/models/{self._model}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if response.is_success:
                    value = _read_context_window(response.json())
                    if value is not None:
                        self._context_window_cache = value
                        return value
        except Exception as exc:
            logger.debug("无法读取模型上下文上限：%s", exc)

        value = known_context_window(self._model)
        self._context_window_cache = value
        return value

    # ──────────────────────────────────────────────
    # 流式对话（支持工具调用收集）
    # ──────────────────────────────────────────────

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """流式调用 LLM。

        每次 yield 一个 StreamChunk：
        - 纯文本回答时：chunk.text 有内容，chunk.tool_calls 为空
        - 工具调用时：先 yield 空文本块，最后一块 chunk.tool_calls 有完整调用列表

        Args:
            messages: 消息历史列表。
            tools: 可选的工具定义列表。
            temperature: 覆盖默认温度。
            max_tokens: 覆盖默认最大 token 数。

        Yields:
            StreamChunk 数据块。

        Raises:
            ModelInvocationError: LLM 调用失败。
        """
        # 转换为 OpenAI 格式的消息
        openai_messages = [msg.to_openai_dict() for msg in messages]
        # 构建请求参数
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": openai_messages,
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "stream": True,
        }
        # 如果有工具定义，添加到请求参数
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        # 调用 OpenAI API
        try:
            logger.debug(
                "LLM stream call: model=%s, messages=%d, tools=%d",
                self._model,
                len(messages),
                len(tools) if tools else 0,
            )
            # 调用 OpenAI API
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise ModelInvocationError(f"OpenAI API stream call failed: {exc}") from exc

        # 收集工具调用的增量数据（各 index 独立积累）
        # tool_call_accum: index -> {"id": str, "name": str, "args": str}
        tool_call_accum: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None

        # 处理流式响应
        try:
            async for chunk in stream:
                # 跳过空 chunk
                if not chunk.choices:
                    continue
                # 提取增量内容
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                # ── 文本内容块，直接 yield ──────────────────────
                if delta.content:
                    yield StreamChunk(text=delta.content)

                # ── 工具调用增量，累积不 yield ─────────────────
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_call_accum:
                            tool_call_accum[idx] = {"id": "", "name": "", "args": ""}

                        if tc_delta.id:
                            tool_call_accum[idx]["id"] += tc_delta.id
                        if tc_delta.function and tc_delta.function.name:
                            tool_call_accum[idx]["name"] += tc_delta.function.name
                        if tc_delta.function and tc_delta.function.arguments:
                            tool_call_accum[idx]["args"] += tc_delta.function.arguments

        except Exception as exc:
            raise ModelInvocationError(f"LLM stream failed: {exc}") from exc

        # 流结束后，如果有工具调用，yield 最终 chunk 携带完整 tool_calls
        if tool_call_accum:
            tool_calls: list[dict[str, Any]] = []
            for idx in sorted(tool_call_accum.keys()):
                acc = tool_call_accum[idx]
                try:
                    args = json.loads(acc["args"]) if acc["args"] else {}
                except json.JSONDecodeError as exc:
                    raise ModelInvocationError(
                        f"OpenAI 流式工具 {acc['name']!r} 参数不是有效 JSON：{exc}"
                    ) from exc
                if not isinstance(args, dict):
                    raise ModelInvocationError(f"OpenAI 流式工具 {acc['name']!r} 参数必须是 JSON 对象")
                # 构建工具调用记录
                tool_calls.append(
                    {
                        "id": acc["id"],
                        "type": "function",
                        "name": acc["name"],
                        "args": args,
                    }
                )
            # 最后一个 chunk，包含完整 tool_calls
            logger.debug(
                "LLM stream end: model=%s, messages=%d, tools=%d",
                self._model,
                len(messages),
                len(tools) if tools else 0,
            )
            yield StreamChunk(
                tool_calls=tool_calls,
                is_tool_call=True,
                finish_reason=finish_reason,
            )
        else:
            # 纯文本结束，发一个 finish 信号
            yield StreamChunk(finish_reason=finish_reason)

    # ──────────────────────────────────────────────
    # JSON 结构化输出
    # ──────────────────────────────────────────────

    async def parse_json(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用 LLM 并解析 JSON 输出。

        Args:
            messages: 消息历史列表。
            schema: 可选的 JSON schema（用于验证）。

        Returns:
            解析后的 JSON 对象。

        Raises:
            ModelInvocationError: LLM 调用失败或 JSON 解析失败。
        """
        # Message 是可变对象，浅拷贝会把 JSON 指令永久追加到调用方的 system 消息。
        enhanced_messages = copy.deepcopy(messages)
        if enhanced_messages and enhanced_messages[0].role == "system":
            enhanced_messages[0].content += "\n\n**重要**：你必须返回有效的 JSON 格式，不要包含任何其他文本。"

        response = await self.chat(enhanced_messages)
        content = response.content.strip()

        # 移除 markdown 代码块标记
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelInvocationError(
                f"Failed to parse JSON from LLM output: {exc}\nOutput: {content[:200]}"
            ) from exc

        if not isinstance(result, dict):
            raise ModelInvocationError(f"LLM output is not a JSON object: {type(result)}")

        if schema is not None:
            from agent_core.errors import ModelOutputValidationError
            from agent_core.model.structured import validate_json_schema

            try:
                validate_json_schema(result, schema)
            except ModelOutputValidationError as exc:
                raise ModelInvocationError(f"LLM JSON 输出不符合 schema：{exc}") from exc

        return {str(key): value for key, value in result.items()}


# ──────────────────────────────────────────────
# 适配器 2：Anthropic Claude
# ──────────────────────────────────────────────


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


# ──────────────────────────────────────────────
# 适配器 3：Gemini
# ──────────────────────────────────────────────


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


# ──────────────────────────────────────────────
# 适配器 4：Ollama（本地模型）
# ──────────────────────────────────────────────


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


# ──────────────────────────────────────────────
# 提供商构造器与注册
# ──────────────────────────────────────────────


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
