"""请求超时传播（Context Deadline）中间件。

确保用户请求的总超时预算逐层传递到每个工具调用，
避免"用户 30s 超时但 MCP 各调 90s"导致总耗时不可控。

工作原理：
1. HTTP 层在请求进入时设置 metadata["deadline"] = time.monotonic() + total_seconds
2. before_tool 检查剩余时间：
   - 剩余时间 < min_tool_timeout → STOP（时间不够，提前终止）
   - 否则 → 将 remaining_budget 写入 metadata 供 executor 使用
3. before_model 检查剩余时间：
   - 剩余时间 < min_iteration_budget → STOP（不再进行下一轮 LLM 调用）

配置：
    AGENT_TIMEOUT_ENABLED: 是否启用（默认 true）
    AGENT_TIMEOUT_TOTAL_SECONDS: 总超时预算秒数（默认 60s，HTTP 层可覆盖）
    AGENT_TIMEOUT_MIN_TOOL_SECONDS: 工具调用的最小超时预算（默认 5s）
    AGENT_TIMEOUT_MIN_ITERATION_SECONDS: 下一轮 LLM 调用的最小预算（默认 10s）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["TimeoutBudgetConfig", "TimeoutBudgetMiddleware"]


@dataclass
class TimeoutBudgetConfig:
    """超时预算配置。"""

    enabled: bool = True
    total_seconds: float = 60.0
    min_tool_seconds: float = 5.0
    min_iteration_seconds: float = 10.0


class TimeoutBudgetMiddleware(Middleware):
    priority = 12
    """超时预算传播：before_model / before_tool 检查剩余时间。"""

    def __init__(self, config: TimeoutBudgetConfig | None = None) -> None:
        self._config = config or TimeoutBudgetConfig()

    def _get_remaining(self, context: MiddlewareContext) -> float | None:
        """获取剩余预算（秒）。返回 None 表示未设置 deadline。"""
        deadline = context.metadata.get("deadline")
        if deadline is None:
            return None
        return max(0.0, deadline - time.monotonic())

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 首次调用：设置 deadline（如果 HTTP 层没有设置）
        if "deadline" not in context.metadata:
            context.metadata["deadline"] = time.monotonic() + self._config.total_seconds
        remaining = self._get_remaining(context)
        if remaining is not None and remaining < self._config.min_iteration_seconds:
            logger.warning("[Timeout] 剩余预算 %.1fs 不足以进行下一轮 LLM 调用（需 >= %.1fs）",
                           remaining, self._config.min_iteration_seconds)
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error=f"请求超时预算已用尽（剩余 {remaining:.1f}s），提前终止",
                data={"timeout_exceeded": True},
            )
        if remaining is not None:
            context.metadata["remaining_budget_seconds"] = round(remaining, 2)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        remaining = self._get_remaining(context)
        if remaining is not None and remaining < self._config.min_tool_seconds:
            logger.warning("[Timeout] 剩余预算 %.1fs 不足以执行工具 %s（需 >= %.1fs）",
                           remaining, context.tool_name, self._config.min_tool_seconds)
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error=f"超时预算不足，跳过工具 {context.tool_name}",
                data={"timeout_exceeded": True},
            )
        if remaining is not None:
            context.metadata["tool_timeout_seconds"] = round(remaining, 1)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)