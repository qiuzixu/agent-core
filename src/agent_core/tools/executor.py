"""手写工具执行器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent_core.ports.storage import ToolOperationStore
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
    priority: int = 100
    phase: str = "generic"



class ToolRegistrationError(Exception):
    """工具注册冲突（同名工具已存在且未显式允许覆盖）。"""

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
        priority: int = 100,
        phase: str = "generic",
        replace: bool = False,
    ) -> None:
        """注册一个工具。

        Args:
            name: 工具名称（全局唯一）。
            func: 工具函数（可以是同步或异步）。
            description: 工具描述（供 LLM 理解用途）。
        """
        if name in self._tools and not replace:
            raise ToolRegistrationError(
                f"工具 {name!r} 已注册，如需覆盖请显式传入 replace=True"
            )
        self._tools[name] = func
        self._specs[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}, "required": []},
            risk_level=risk_level,
            requires_approval=requires_approval,
            timeout_seconds=timeout_seconds,
            idempotent=idempotent,
            priority=max(0, int(priority)),
            phase=phase,
        )
        logger.debug("Registered tool: %r", name)

    def unregister(self, name: str) -> None:
        """移除一个动态注册的工具；不存在时静默返回。"""
        self._tools.pop(name, None)
        self._specs.pop(name, None)

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
        for name, spec in sorted(
            self._specs.items(),
            key=lambda item: (item[1].priority, item[0]),
        ):
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
        max_concurrent: int = 5,
        operation_store: ToolOperationStore | None = None,
    ) -> None:
        self._registry = registry
        self._approval_callback = approval_callback
        self._operation_store = operation_store
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        # 工具使用统计：{tool_name: {"calls": int, "successes": int, "failures": int, "total_seconds": float}}
        self._usage_stats: dict[str, dict[str, Any]] = {}

    def spec_for(self, tool_name: str) -> ToolSpec | None:
        """返回工具元数据，供 Agent Loop 做优先级调度。"""
        return self._registry.get_spec(tool_name)

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        approval_granted: bool | None = None,
        operation_id: str | None = None,
    ) -> str:
        """执行单个工具调用。

        Args:
            tool_name: 工具名称。
            arguments: 工具参数。

        Returns:
            工具执行结果（字符串格式）。
        """
        result = await self.execute_result(
            tool_name,
            arguments,
            approval_granted=approval_granted,
            operation_id=operation_id,
        )
        return result.to_text()

    # TODO(debug): breakpoint suggestion #6 - execute_result: tool actual
    # execution point (param validation -> timeout/approval -> business call).
    async def execute_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        approval_granted: bool | None = None,
        operation_id: str | None = None,
    ) -> ToolResult:
        """执行工具并返回结构化结果。

        ``approval_granted`` 用于已经在 API 或工作流边界完成审批的调用。
        未显式传入时仍使用构造器注入的审批回调，避免普通 Agent Loop 绕过审批。
        """
        async with self._semaphore:
            return await self._execute_result(
                tool_name,
                arguments,
                approval_granted=approval_granted,
                operation_id=operation_id,
            )

    async def _execute_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        approval_granted: bool | None = None,
        operation_id: str | None = None,
    ) -> ToolResult:
        """在并发限额内执行单个工具调用。"""
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
        if spec is not None and not spec.idempotent and not operation_id:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error="非幂等工具必须提供 operation_id",
                error_kind="invalid_arguments",
            )
        if spec is not None and not spec.idempotent and self._operation_store is None:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error="非幂等工具必须配置 ToolOperationStore",
                error_kind="execution",
                retryable=True,
                operation_id=operation_id,
            )
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
                if approval_granted is True:
                    pass
                elif approval_granted is False:
                    return ToolResult(
                        tool_name=tool_name,
                        success=False,
                        error="审批拒绝",
                        error_kind="approval",
                    )
                elif self._approval_callback is None:
                    return ToolResult(
                        tool_name=tool_name,
                        success=False,
                        error="未配置审批回调",
                        error_kind="approval",
                    )
                else:
                    try:
                        approved = await self._approval_callback(spec, arguments)
                    except Exception as exc:
                        logger.warning("工具 %r 审批回调失败：%s", tool_name, exc, exc_info=True)
                        return ToolResult(
                            tool_name=tool_name,
                            success=False,
                            error=f"审批回调失败：{exc}",
                            error_kind="approval",
                            retryable=True,
                        )
                    if not approved:
                        return ToolResult(
                            tool_name=tool_name,
                            success=False,
                            error="审批拒绝",
                            error_kind="approval",
                        )

        import time
        arguments_hash = hashlib.sha256(
            json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        operation_lock = (
            self._operation_locks.setdefault(operation_id, asyncio.Lock())
            if operation_id
            else None
        )
        operation_claimed = False
        operation_saving = False
        _start_time = time.monotonic()
        try:
            logger.debug("Executing tool: %r with args: %s", tool_name, arguments)

            async def invoke() -> Any:
                if asyncio.iscoroutinefunction(tool_func):
                    return await tool_func(**arguments)
                # 同步工具放到线程，避免阻塞 Agent Loop。
                return await asyncio.to_thread(tool_func, **arguments)

            async def invoke_once() -> ToolResult:
                nonlocal operation_claimed, operation_saving
                if self._operation_store is not None and operation_id is not None:
                    claim = getattr(self._operation_store, "claim", None)
                    if callable(claim):
                        owner, cached = await claim(
                            operation_id,
                            tool_name=tool_name,
                            arguments_hash=arguments_hash,
                        )
                        if not owner and cached is None:
                            return ToolResult(
                                tool_name=tool_name,
                                success=False,
                                error="相同 operation_id 的工具操作正在执行",
                                error_kind="execution",
                                retryable=True,
                                operation_id=operation_id,
                            )
                        operation_claimed = owner
                    else:
                        cached = await self._operation_store.get(
                            operation_id,
                            tool_name=tool_name,
                            arguments_hash=arguments_hash,
                        )
                    if cached is not None:
                        cached.operation_id = operation_id
                        return cached
                if spec and spec.timeout_seconds:
                    value = await asyncio.wait_for(invoke(), spec.timeout_seconds)
                else:
                    value = await invoke()
                if isinstance(value, ToolResult):
                    outcome = value
                    outcome.tool_name = tool_name
                else:
                    outcome = ToolResult(tool_name=tool_name, success=True, value=value)
                outcome.operation_id = operation_id
                if self._operation_store is not None and operation_id is not None and outcome.success:
                    operation_saving = True
                    await self._operation_store.save(
                        operation_id,
                        outcome,
                        tool_name=tool_name,
                        arguments_hash=arguments_hash,
                    )
                    operation_saving = False
                elif operation_claimed:
                    release = getattr(self._operation_store, "release", None)
                    if callable(release):
                        await release(operation_id or "")
                    operation_claimed = False
                return outcome

            if operation_lock is None:
                outcome = await invoke_once()
            else:
                async with operation_lock:
                    outcome = await invoke_once()

            self._record_usage(tool_name, time.monotonic() - _start_time, success=True)
            if not outcome.success and not outcome.error_kind:
                outcome.error_kind = "execution"
            return outcome

        except TimeoutError:
            if operation_claimed and not operation_saving:
                release = getattr(self._operation_store, "release", None)
                if callable(release):
                    await release(operation_id or "")
            self._record_usage(tool_name, time.monotonic() - _start_time, success=False)
            logger.warning("Tool %r timed out", tool_name)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error="工具执行超时",
                error_kind="timeout",
                retryable=True,
            )
        except Exception as exc:
            if operation_claimed and not operation_saving:
                release = getattr(self._operation_store, "release", None)
                if callable(release):
                    await release(operation_id or "")
            self._record_usage(tool_name, time.monotonic() - _start_time, success=False)
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
            if name not in properties and schema.get("additionalProperties") is False:
                return f"参数 {name} 未在工具 schema 中声明"
            definition = properties.get(name, {})
            expected = definition.get("type")
            if expected == "string" and not isinstance(value, str):
                return f"参数 {name} 必须是字符串"
            if expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                return f"参数 {name} 必须是数字"
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                return f"参数 {name} 必须是整数"
            if expected == "boolean" and not isinstance(value, bool):
                return f"参数 {name} 必须是布尔值"
            if expected == "array" and not isinstance(value, list):
                return f"参数 {name} 必须是数组"
            if expected == "object" and not isinstance(value, dict):
                return f"参数 {name} 必须是对象"
        return None

    def _record_usage(self, tool_name: str, elapsed_seconds: float, *, success: bool) -> None:
        """\u8bb0\u5f55\u5de5\u5177\u8c03\u7528\u7edf\u8ba1\u3002"""
        stats = self._usage_stats.setdefault(
            tool_name, {"calls": 0, "successes": 0, "failures": 0, "total_seconds": 0.0}
        )
        stats["calls"] += 1
        if success:
            stats["successes"] += 1
        else:
            stats["failures"] += 1
        stats["total_seconds"] += elapsed_seconds

    def get_usage_stats(self) -> dict[str, dict[str, Any]]:
        """\u8fd4\u56de\u5de5\u5177\u4f7f\u7528\u7edf\u8ba1\u5feb\u7167\u3002"""
        return {name: dict(stats) for name, stats in self._usage_stats.items()}

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
        async def _limited_execute(call: dict[str, Any]) -> str:
            return await self.execute(
                call["name"],
                call.get("args", {}),
                operation_id=call.get("operation_id"),
            )

        tasks = [_limited_execute(tc) for tc in tool_calls]
        return await asyncio.gather(*tasks)
