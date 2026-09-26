"""手写工具执行器。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent_core.protocol.runtime import ToolResult

logger = logging.getLogger(__name__)

# 工具函数签名
ToolFunc = Callable[..., Any]


@dataclass(frozen=True)
class ToolSpec:
    """工具的可执行元数据，和工具函数本身分离。"""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "read"
    requires_approval: bool = False
    timeout_seconds: float | None = None
    idempotent: bool = True


class ToolRegistry:
    """工具注册表。

    管理所有可用的工具函数。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolFunc] = {}
        self._specs: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        func: ToolFunc,
        description: str,
        *,
        parameters: dict[str, Any] | None = None,
        risk_level: str = "read",
        requires_approval: bool = False,
        timeout_seconds: float | None = None,
        idempotent: bool = True,
    ) -> None:
        """注册一个工具。

        Args:
            name: 工具名称（全局唯一）。
            func: 工具函数（可以是同步或异步）。
            description: 工具描述（供 LLM 理解用途）。
        """
        if name in self._tools:
            logger.warning("Tool %r already registered, overwriting", name)
        self._tools[name] = func
        self._specs[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}, "required": []},
            risk_level=risk_level,
            requires_approval=requires_approval,
            timeout_seconds=timeout_seconds,
            idempotent=idempotent,
        )
        logger.debug("Registered tool: %r", name)

    def get_tool(self, name: str) -> ToolFunc | None:
        """获取工具函数。"""
        return self._tools.get(name)

    def get_all_tools(self) -> dict[str, ToolFunc]:
        """获取所有工具。"""
        return self._tools.copy()

    def get_tool_names(self) -> list[str]:
        """获取所有工具名称。"""
        return list(self._tools.keys())

    def get_spec(self, name: str) -> ToolSpec | None:
        """获取工具策略和参数 schema。"""
        return self._specs.get(name)

    def get_all_specs(self) -> dict[str, ToolSpec]:
        return self._specs.copy()

    def build_tool_definitions(self) -> list[dict[str, Any]]:
        """构造 OpenAI function calling 格式的工具定义列表。

        返回注册时声明的完整参数 schema。
        """
        definitions = []
        for name, spec in self._specs.items():
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
            )
        return definitions


class ToolExecutor:
    """工具执行器。

    负责根据 tool_call 执行对应的工具函数。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        approval_callback: Callable[[ToolSpec, dict[str, Any]], Awaitable[bool]] | None = None,
    ) -> None:
        self._registry = registry
        self._approval_callback = approval_callback

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        """执行单个工具调用。

        Args:
            tool_name: 工具名称。
            arguments: 工具参数。

        Returns:
            工具执行结果（字符串格式）。
        """
        result = await self.execute_result(tool_name, arguments)
        return result.to_text()

    async def execute_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        """执行工具并返回结构化结果，旧的 execute 保留文本兼容层。"""
        tool_func = self._registry.get_tool(tool_name)
        if tool_func is None:
            error_msg = f"Tool {tool_name!r} not found."
            logger.warning(error_msg)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=error_msg,
                error_kind="not_found",
            )

        if not isinstance(arguments, dict):
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error="arguments 必须是对象",
                error_kind="invalid_arguments",
            )

        spec = self._registry.get_spec(tool_name)
        if spec is not None:
            schema_error = self._validate_arguments(spec, arguments)
            if schema_error:
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    error=schema_error,
                    error_kind="invalid_arguments",
                )
            if spec.requires_approval:
                if self._approval_callback is None:
                    return ToolResult(
                        tool_name=tool_name,
                        success=False,
                        error="未配置审批回调",
                        error_kind="approval",
                    )
                approved = await self._approval_callback(spec, arguments)
                if not approved:
                    return ToolResult(
                        tool_name=tool_name,
                        success=False,
                        error="审批拒绝",
                        error_kind="approval",
                    )

        try:
            logger.debug("Executing tool: %r with args: %s", tool_name, arguments)

            async def invoke() -> Any:
                if asyncio.iscoroutinefunction(tool_func):
                    return await tool_func(**arguments)
                # 同步工具放到线程，避免阻塞 Agent Loop。
                return await asyncio.to_thread(tool_func, **arguments)

            if spec and spec.timeout_seconds:
                result = await asyncio.wait_for(invoke(), spec.timeout_seconds)
            else:
                result = await invoke()

            return ToolResult(tool_name=tool_name, success=True, value=result)

        except TimeoutError:
            logger.warning("Tool %r timed out", tool_name)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error="工具执行超时",
                error_kind="timeout",
                retryable=True,
            )
        except Exception as exc:
            logger.warning("Tool %r raised exception: %s", tool_name, exc, exc_info=True)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=str(exc),
                error_kind="execution",
                retryable=False,
            )

    @staticmethod
    def _validate_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
        """执行一层无第三方依赖的基本 schema 校验。"""
        schema = spec.parameters or {}
        required = schema.get("required", [])
        missing = [name for name in required if name not in arguments]
        if missing:
            return f"缺少必填参数：{', '.join(str(item) for item in missing)}"
        properties = schema.get("properties", {})
        for name, value in arguments.items():
            definition = properties.get(name, {})
            expected = definition.get("type")
            if expected == "string" and not isinstance(value, str):
                return f"参数 {name} 必须是字符串"
            if expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                return f"参数 {name} 必须是数字"
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                return f"参数 {name} 必须是整数"
        return None

    async def execute_batch(
        self,
        tool_calls: list[dict[str, Any]],
    ) -> list[str]:
        """并发执行多个工具调用。

        Args:
            tool_calls: 工具调用列表，每个包含 name 和 args。

        Returns:
            工具执行结果列表（与 tool_calls 顺序对应）。
        """
        tasks = [self.execute(tc["name"], tc.get("args", {})) for tc in tool_calls]
        return await asyncio.gather(*tasks)
