"""外部服务熔断器中间件。

为 MCP / Business API 等外部 HTTP 调用提供独立的熔断保护。
当某个外部服务连续失败超过阈值时自动熔断，
在冷却时间后允许半开探测，成功则恢复。

状态机：
    CLOSED → 连续失败 >= threshold → OPEN
    OPEN   → 等待 cooldown → HALF_OPEN
    HALF_OPEN → 探测成功 → CLOSED
    HALF_OPEN → 探测失败 → OPEN（重置冷却时间）
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["CircuitBreakerConfig", "CircuitBreakerMiddleware", "CircuitState", "CircuitBreakerRegistry"]


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """单个外部服务的熔断配置。"""

    name: str
    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    half_open_max_calls: int = 3
    # 匹配哪些工具名（前缀匹配）
    tool_name_prefixes: list[str] = field(default_factory=list)


@dataclass
class _CircuitState:
    """运行时熔断状态。"""

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    half_open_calls: int = 0
    total_failures: int = 0
    total_successes: int = 0
    total_rejects: int = 0


class CircuitBreakerRegistry:
    """管理多个外部服务的熔断器。"""

    def __init__(self, configs: list[CircuitBreakerConfig] | None = None) -> None:
        self._configs: dict[str, CircuitBreakerConfig] = {}
        self._states: dict[str, _CircuitState] = {}
        self._lock = asyncio.Lock()
        for cfg in configs or []:
            self.register(cfg)

    def register(self, config: CircuitBreakerConfig) -> None:
        self._configs[config.name] = config
        self._states[config.name] = _CircuitState()

    def get_state(self, name: str) -> CircuitState:
        state = self._states.get(name)
        return state.state if state else CircuitState.CLOSED

    def get_stats(self) -> dict[str, dict[str, Any]]:
        """返回所有熔断器的状态快照（供 /system 端点暴露）。"""
        result = {}
        for name, state in self._states.items():
            result[name] = {
                "state": state.state.value,
                "failure_count": state.failure_count,
                "total_failures": state.total_failures,
                "total_successes": state.total_successes,
                "total_rejects": state.total_rejects,
            }
        return result

    async def check(self, service_name: str) -> bool:
        """检查是否允许调用该服务。返回 False 表示熔断拒绝。"""
        async with self._lock:
            cfg = self._configs.get(service_name)
            state = self._states.get(service_name)
            if not cfg or not state:
                return True  # 未注册的服务不做熔断

            now = time.monotonic()

            if state.state == CircuitState.OPEN:
                elapsed = now - state.last_failure_time
                if elapsed >= cfg.cooldown_seconds:
                    # 冷却结束，进入半开
                    state.state = CircuitState.HALF_OPEN
                    state.half_open_calls = 0
                    logger.info("[CircuitBreaker] %s: OPEN → HALF_OPEN (冷却 %.1fs 结束)", service_name, elapsed)
                else:
                    state.total_rejects += 1
                    logger.warning(
                        "[CircuitBreaker] %s: 熔断中，拒绝调用 (剩余 %.1fs)",
                        service_name,
                        cfg.cooldown_seconds - elapsed,
                    )
                    return False

            if state.state == CircuitState.HALF_OPEN:
                if state.half_open_calls >= cfg.half_open_max_calls:
                    state.total_rejects += 1
                    return False
                state.half_open_calls += 1

            return True

    async def record_success(self, service_name: str) -> None:
        """记录成功调用。"""
        async with self._lock:
            state = self._states.get(service_name)
            if not state:
                return
            state.total_successes += 1
            if state.state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
                state.state = CircuitState.CLOSED
                state.failure_count = 0
                logger.info("[CircuitBreaker] %s: → CLOSED (探测成功)", service_name)
            elif state.state == CircuitState.CLOSED:
                state.failure_count = 0  # 重置连续失败计数

    async def record_failure(self, service_name: str) -> None:
        """记录失败调用。"""
        async with self._lock:
            cfg = self._configs.get(service_name)
            state = self._states.get(service_name)
            if not cfg or not state:
                return
            state.total_failures += 1
            state.failure_count += 1
            state.last_failure_time = time.monotonic()

            if state.state == CircuitState.HALF_OPEN:
                # 半开探测失败，重新熔断
                state.state = CircuitState.OPEN
                logger.warning("[CircuitBreaker] %s: HALF_OPEN → OPEN (探测失败)", service_name)
            elif state.state == CircuitState.CLOSED and state.failure_count >= cfg.failure_threshold:
                state.state = CircuitState.OPEN
                logger.warning(
                    "[CircuitBreaker] %s: CLOSED → OPEN (连续失败 %d 次)",
                    service_name,
                    state.failure_count,
                )


class CircuitBreakerMiddleware(Middleware):
    priority = 10
    """Agent 中间件层的外部服务熔断。

    在 before_tool 阶段检查目标工具对应的外部服务是否熔断。
    在 after_tool 阶段记录成功/失败。
    """

    def __init__(self, registry: CircuitBreakerRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> CircuitBreakerRegistry:
        return self._registry

    def _find_service(self, tool_name: str | None) -> str | None:
        """根据工具名找到对应的外部服务配置。"""
        if not tool_name:
            return None
        for name, cfg in self._registry._configs.items():
            for prefix in cfg.tool_name_prefixes:
                if tool_name.startswith(prefix):
                    return name
        return None

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        service = self._find_service(context.tool_name)
        if not service:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        allowed = await self._registry.check(service)
        if not allowed:
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error=f"服务 {service} 熔断中，工具 {context.tool_name} 暂不可用",
            )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        service = self._find_service(context.tool_name)
        if not service:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 判断工具执行是否成功（简单：有结果且无 error metadata）
        tool_ok = context.tool_result is not None and not context.metadata.get("tool_error")
        if tool_ok:
            await self._registry.record_success(service)
        else:
            await self._registry.record_failure(service)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
