"""成本追踪 / Token 预算中间件。

按用户/租户维度追踪 LLM 调用的 token 消耗和估算成本，
当超过预算阈值时拒绝后续调用（STOP），防止 token 失控。

配置：
    AGENT_COST_BUDGET_ENABLED: 是否启用（默认 false）
    AGENT_COST_BUDGET_DEFAULT_TOKENS: 每用户默认 token 预算（默认 1_000_000）
    AGENT_COST_BUDGET_RESET_HOURS: 预算重置周期（默认 24h）
    AGENT_COST_INPUT_PRICE_PER_1K: 输入 token 单价（每千 token，美元，默认 0.001）
    AGENT_COST_OUTPUT_PRICE_PER_1K: 输出 token 单价（每千 token，美元，默认 0.002）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CostBudgetConfig",
    "CostBudgetMiddleware",
    "CostRecord",
    "UserCostTracker",
]


@dataclass
class CostBudgetConfig:
    """成本追踪配置。"""

    enabled: bool = False
    default_budget_tokens: int = 1_000_000
    reset_hours: float = 24.0
    # 单价（美元 / 千 token），默认值仅用于估算
    input_price_per_1k: float = 0.001
    output_price_per_1k: float = 0.002


@dataclass
class CostRecord:
    """单次调用成本记录。"""

    timestamp: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    model: str = ""
    iteration: int = 0


@dataclass
class UserBudget:
    """单个用户的预算状态。"""

    user_id: str
    budget_tokens: int
    used_input_tokens: int = 0
    used_output_tokens: int = 0
    used_cost_usd: float = 0.0
    records: list[CostRecord] = field(default_factory=list)
    window_start: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.used_input_tokens + self.used_output_tokens

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.budget_tokens - self.total_tokens)

    @property
    def usage_ratio(self) -> float:
        if self.budget_tokens <= 0:
            return 0.0
        return self.total_tokens / self.budget_tokens


class UserCostTracker:
    """进程内用户成本追踪器。生产环境可替换为 Redis / DB 后端。"""

    def __init__(self, config: CostBudgetConfig | None = None) -> None:
        self._config = config or CostBudgetConfig()
        self._budgets: dict[str, UserBudget] = {}

    def _get_budget(self, user_id: str) -> UserBudget:
        now = time.time()
        budget = self._budgets.get(user_id)
        if budget is None:
            budget = UserBudget(
                user_id=user_id,
                budget_tokens=self._config.default_budget_tokens,
                window_start=now,
            )
            self._budgets[user_id] = budget
        # 窗口重置
        elif (now - budget.window_start) > self._config.reset_hours * 3600:
            logger.info("[CostBudget] 用户 %s 预算窗口重置", user_id)
            budget.used_input_tokens = 0
            budget.used_output_tokens = 0
            budget.used_cost_usd = 0.0
            budget.records.clear()
            budget.window_start = now
        return budget

    def check_budget(self, user_id: str) -> tuple[bool, str, UserBudget]:
        """检查用户是否还有预算。Returns (allowed, reason, budget)。"""
        budget = self._get_budget(user_id)
        if budget.remaining_tokens <= 0:
            return False, f"用户 {user_id} token 预算已耗尽 ({budget.budget_tokens})", budget
        return True, "", budget

    def record_usage(
        self,
        user_id: str,
        input_tokens: int,
        output_tokens: int,
        model: str = "",
        iteration: int = 0,
    ) -> CostRecord:
        """记录一次 LLM 调用的 token 消耗。"""
        budget = self._get_budget(user_id)
        cfg = self._config
        cost = (
            input_tokens / 1000 * cfg.input_price_per_1k
            + output_tokens / 1000 * cfg.output_price_per_1k
        )
        record = CostRecord(
            timestamp=time.time(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            model=model,
            iteration=iteration,
        )
        budget.used_input_tokens += input_tokens
        budget.used_output_tokens += output_tokens
        budget.used_cost_usd += cost
        budget.records.append(record)
        # 防止 records 无限增长
        if len(budget.records) > 10_000:
            budget.records = budget.records[-5_000:]
        return record

    def get_usage(self, user_id: str) -> dict[str, Any]:
        """获取用户当前用量摘要（供 /system 端点）。"""
        budget = self._get_budget(user_id)
        return {
            "user_id": user_id,
            "budget_tokens": budget.budget_tokens,
            "used_tokens": budget.total_tokens,
            "remaining_tokens": budget.remaining_tokens,
            "usage_ratio": round(budget.usage_ratio, 4),
            "used_cost_usd": round(budget.used_cost_usd, 6),
            "window_start": budget.window_start,
        }

    def set_budget(self, user_id: str, budget_tokens: int) -> None:
        """动态设置用户预算。"""
        budget = self._get_budget(user_id)
        budget.budget_tokens = budget_tokens


class CostBudgetMiddleware(Middleware):
    priority = 14
    """Agent 中间件层的成本追踪与预算控制。

    before_model: 检查用户是否还有预算
    after_model:  记录本次调用的 token 消耗
    """

    def __init__(
        self,
        config: CostBudgetConfig | None = None,
        tracker: UserCostTracker | None = None,
    ) -> None:
        self._config = config or CostBudgetConfig()
        self._tracker = tracker or UserCostTracker(self._config)

    @property
    def tracker(self) -> UserCostTracker:
        return self._tracker

    def _get_user_id(self, context: MiddlewareContext) -> str:
        return context.metadata.get("user_id", "anonymous")

    def _estimate_tokens(self, context: MiddlewareContext) -> tuple[int, int]:
        """从 metadata 中提取 token 用量，或从消息长度估算。"""
        usage = context.metadata.get("token_usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        if input_tokens == 0 and context.messages:
            # 粗估：4 字符 ≈ 1 token
            total_chars = sum(len(str(getattr(m, "content", "") or "")) for m in context.messages)
            input_tokens = total_chars // 4
        if output_tokens == 0 and context.llm_response:
            content = str(getattr(context.llm_response, "content", "") or "")
            output_tokens = len(content) // 4
        return input_tokens, output_tokens

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        user_id = self._get_user_id(context)
        allowed, reason, budget = self._tracker.check_budget(user_id)
        if not allowed:
            logger.warning("[CostBudget] %s", reason)
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error=reason,
                data={"cost_budget_exceeded": True},
            )
        # 如果用量超过 80%，打警告
        if budget.usage_ratio > 0.8:
            logger.warning(
                "[CostBudget] 用户 %s token 用量已达 %.1f%% (%d/%d)",
                user_id,
                budget.usage_ratio * 100,
                budget.total_tokens,
                budget.budget_tokens,
            )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        user_id = self._get_user_id(context)
        input_tokens, output_tokens = self._estimate_tokens(context)
        if input_tokens > 0 or output_tokens > 0:
            model = context.metadata.get("model_name", "")
            self._tracker.record_usage(
                user_id=user_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
                iteration=context.iteration,
            )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
