"""工具结果 Schema 校验中间件。

在 after_tool 阶段验证工具返回的数据是否符合预期的 JSON Schema。
防止 MCP 返回畸形数据直接进入 LLM 上下文导致幻觉或错误。

支持两种校验方式：
1. 显式注册：register_schema("tool_name", schema_dict)
2. 自动检测：如果工具返回的是 JSON，检查是否可解析且有非空结构

配置：
    AGENT_TOOL_VALIDATION_ENABLED: 是否启用（默认 true）
    AGENT_TOOL_VALIDATION_STRICT: 严格模式（校验失败时 STOP 而不是仅警告，默认 false）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["ToolValidationConfig", "ToolValidationMiddleware"]


@dataclass
class ToolValidationConfig:
    """工具结果校验配置。"""

    enabled: bool = True
    strict: bool = False  # True = 校验失败 STOP，False = 警告但继续
    # 工具名 → JSON Schema
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 不校验的工具列表
    excluded_tools: set[str] = field(default_factory=lambda: {"get_time", "noop"})


class ToolValidationMiddleware(Middleware):
    """在 after_tool 阶段验证工具返回的数据。

    使用轻量 JSON Schema 子集校验（type / required / properties），
    不引入 jsonschema 外部依赖（保持 agent-core 零依赖原则）。
    """

    def __init__(self, config: ToolValidationConfig | None = None) -> None:
        self._config = config or ToolValidationConfig()

    def register_schema(self, tool_name: str, schema: dict[str, Any]) -> None:
        """注册工具的期望 Schema。"""
        self._config.schemas[tool_name] = schema

    def _validate_json_schema(
        self, data: Any, schema: dict[str, Any], path: str = "$"
    ) -> list[str]:
        """轻量 JSON Schema 校验（子集：type / required / properties / items / enum）。"""
        errors: list[str] = []
        expected_type = schema.get("type")
        if expected_type:
            type_map = {
                "object": dict, "array": list, "string": str,
                "number": (int, float), "integer": int,
                "boolean": bool, "null": type(None),
            }
            py_type = type_map.get(expected_type)
            if py_type and not isinstance(data, py_type):
                # bool 是 int 的子类，需要排除
                if not (expected_type in ("number", "integer") and isinstance(data, bool)):
                    errors.append(f"{path}: 期望 {expected_type}, 实际 {type(data).__name__}")
                    return errors

        if isinstance(data, dict) and "properties" in schema:
            for req_key in schema.get("required", []):
                if req_key not in data:
                    errors.append(f"{path}: 缺少必需字段 '{req_key}'")
            props = schema.get("properties", {})
            for key, sub_schema in props.items():
                if key in data:
                    errors.extend(self._validate_json_schema(data[key], sub_schema, f"{path}.{key}"))

        if isinstance(data, list) and "items" in schema:
            for i, item in enumerate(data):
                errors.extend(self._validate_json_schema(item, schema["items"], f"{path}[{i}]"))

        if "enum" in schema and data not in schema["enum"]:
            errors.append(f"{path}: 值 '{data}' 不在枚举 {schema['enum']} 中")

        return errors

    def _try_parse_json(self, text: str) -> Any | None:
        """尝试解析工具结果为 JSON。"""
        if not text or not isinstance(text, str):
            return None
        stripped = text.strip()
        if not (stripped.startswith("{") or stripped.startswith("[")):
            return None
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        tool_name = context.tool_name or ""
        if tool_name in self._config.excluded_tools:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        result = context.tool_result
        if result is None:
            # 工具没有返回结果，交给错误处理
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        errors: list[str] = []
        schema = self._config.schemas.get(tool_name)

        if schema:
            # 有显式注册的 Schema，做完整校验
            parsed = self._try_parse_json(result)
            if parsed is not None:
                errors = self._validate_json_schema(parsed, schema)
            elif not isinstance(result, dict):
                errors.append(f"{tool_name}: 返回不是有效 JSON，无法按 Schema 校验")
        else:
            # 没有 Schema，做基础健全性检查
            parsed = self._try_parse_json(result)
            if parsed is None and isinstance(result, str) and result.strip().startswith("{"):
                errors.append(f"{tool_name}: 返回看起来是 JSON 但无法解析（可能是截断或格式错误）")
            if isinstance(result, str) and len(result) > 100_000:
                errors.append(f"{tool_name}: 返回异常大 ({len(result)} 字符)，可能包含错误信息或数据泄漏")

        if errors:
            warning = f"[ToolValidation] {tool_name}: {'; '.join(errors[:5])}"
            if self._config.strict:
                logger.error(warning)
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    error=warning,
                )
            logger.warning(warning)
            # 非严格模式：记录警告到 metadata 供后续层感知
            context.metadata.setdefault("tool_validation_warnings", []).extend(errors)

        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
