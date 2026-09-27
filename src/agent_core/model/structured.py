"""提供商无关的结构化输出约束、解析和校验。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from agent_core.errors import ModelOutputValidationError
from agent_core.model.core import ModelAdapter
from agent_core.protocol.messages import Message, system_message, user_message

_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class StructuredOutputSpec[T]:
    """一次结构化输出调用的 Schema 和解析规则。"""

    name: str
    schema: dict[str, Any]
    decoder: Callable[[Any], T] | None = None
    max_retries: int = 1
    instruction: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("结构化输出名称不能为空")
        if self.max_retries < 0:
            raise ValueError("max_retries 不能小于 0")
        if not isinstance(self.schema, dict):
            raise TypeError("schema 必须是 JSON Schema 字典")


@dataclass(frozen=True)
class StructuredOutputResult[T]:
    """结构化输出结果及其原始模型消息。"""

    value: T
    raw_message: Message
    attempts: int


def _extract_json(content: str) -> Any:
    text = content.strip()
    fenced = _JSON_FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelOutputValidationError(
            f"模型输出不是有效 JSON：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
        ) from exc


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ModelOutputValidationError(f"暂不支持的 JSON Schema type：{expected}")


def validate_json_schema(value: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    """校验 Core 支持的 JSON Schema 子集。

    支持 type、required、properties、additionalProperties、items、enum、
    minLength、maxLength、minimum 和 maximum。复杂组合规则可由 decoder 做二次校验。
    """

    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches_type(value, item) for item in expected):
            raise ModelOutputValidationError(f"{path} 类型不匹配，期望 {expected}")
    elif isinstance(expected, str) and not _matches_type(value, expected):
        raise ModelOutputValidationError(f"{path} 类型不匹配，期望 {expected}")

    if "enum" in schema and value not in schema["enum"]:
        raise ModelOutputValidationError(f"{path} 不在允许值 {schema['enum']} 中")

    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise ModelOutputValidationError(f"{path} 缺少必填字段：{', '.join(missing)}")
        properties = schema.get("properties", {})
        for name, item in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                validate_json_schema(item, child_schema, path=f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                raise ModelOutputValidationError(f"{path} 包含未声明字段：{name}")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate_json_schema(item, schema["items"], path=f"{path}[{index}]")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ModelOutputValidationError(f"{path} 长度小于 {schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ModelOutputValidationError(f"{path} 长度大于 {schema['maxLength']}")

    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ModelOutputValidationError(f"{path} 小于最小值 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ModelOutputValidationError(f"{path} 大于最大值 {schema['maximum']}")


def parse_structured_output[T](message: Message, spec: StructuredOutputSpec[T]) -> T:
    """解析并校验一个模型消息。"""

    if message.tool_calls:
        raise ModelOutputValidationError("结构化输出阶段不允许返回工具调用")
    value = _extract_json(message.content)
    validate_json_schema(value, spec.schema)
    if spec.decoder is None:
        return cast(T, value)
    try:
        return spec.decoder(value)
    except ModelOutputValidationError:
        raise
    except Exception as exc:
        raise ModelOutputValidationError(f"结构化输出 decoder 校验失败：{exc}") from exc


def _instruction(spec: StructuredOutputSpec[Any]) -> str:
    if spec.instruction:
        return spec.instruction
    schema = json.dumps(spec.schema, ensure_ascii=False, separators=(",", ":"))
    return (
        f"请只输出符合 JSON Schema 的 JSON 对象，不要使用 Markdown 代码块。"
        f"输出名称：{spec.name}。JSON Schema：{schema}"
    )


async def chat_structured[T](
    model: ModelAdapter,
    messages: list[Message],
    spec: StructuredOutputSpec[T],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> StructuredOutputResult[T]:
    """调用模型并在解析失败时携带错误信息进行有限重试。"""

    # 合并为一条 system 消息，避免 Anthropic 等单 system Provider 丢失应用原提示词。
    system_parts = [message.content for message in messages if message.role == "system"]
    system_parts.append(_instruction(spec))
    working = [
        system_message("\n\n".join(part for part in system_parts if part)),
        *(message for message in messages if message.role != "system"),
    ]
    last_error: ModelOutputValidationError | None = None
    for attempt in range(1, spec.max_retries + 2):
        kwargs: dict[str, Any] = {"tools": tools}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        raw = await model.chat(working, **kwargs)
        try:
            value = parse_structured_output(raw, spec)
            return StructuredOutputResult(value=value, raw_message=raw, attempts=attempt)
        except ModelOutputValidationError as exc:
            last_error = exc
            if attempt > spec.max_retries:
                break
            working.extend(
                [
                    raw,
                    user_message(f"上一次输出校验失败：{exc}。请重新输出完整且合法的 JSON。"),
                ]
            )
    assert last_error is not None
    raise last_error


__all__ = [
    "StructuredOutputResult",
    "StructuredOutputSpec",
    "chat_structured",
    "parse_structured_output",
    "validate_json_schema",
]
