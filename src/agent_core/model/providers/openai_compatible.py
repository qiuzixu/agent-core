"""适配器 1：OpenAI / 兼容接口（默认）。

DashScope(Qwen)、Ollama 等 OpenAI 兼容端点复用该实现；
Qwen 本地 tokenizer 与 tokenizer API 探测辅助也位于本模块。
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from typing import Any

from agent_core.errors import ModelInvocationError
from agent_core.model.core import ContextUsage, StreamChunk
from agent_core.model.providers.common import known_context_window, logger
from agent_core.protocol.messages import Message, assistant_message


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
