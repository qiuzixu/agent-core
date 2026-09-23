"""中间件机制实现。

中间件可以在以下时机注入逻辑：
1. before_model : LLM 调用前
2. after_model  : LLM 调用后
3. before_tool  : 工具执行前
4. after_tool   : 工具执行后

用法：
    agent = ReActAgent(
        llm=llm,
        tool_executor=executor,
        middleware=[
            LoggingMiddleware(),
            RetryMiddleware(max_retries=3),
            TokenLimitMiddleware(max_tokens=4000),
            HumanInTheLoopMiddleware(approval_needed=["takeoff_drone"]),
        ],
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_core.protocol.messages import Message
from agent_core.errors import (
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 基础数据类型
# ──────────────────────────────────────────────

class MiddlewareAction(Enum):
    CONTINUE = "continue"   # 继续执行
    STOP     = "stop"       # 停止执行（中止整个循环）
    MODIFY   = "modify"     # 修改数据（如裁剪消息历史）
    RETRY    = "retry"      # 重试当前 LLM 调用
    ERROR    = "error"      # 返回错误


@dataclass
class MiddlewareResult:
    action: MiddlewareAction
    data:   dict[str, Any] = field(default_factory=dict)
    error:  str | None = None


@dataclass
class MiddlewareContext:
    messages:     list[Message]      = field(default_factory=list)
    iteration:    int                = 0
    tool_name:    str | None         = None
    tool_args:    dict[str, Any]     = field(default_factory=dict)
    tool_result:  str | None         = None
    llm_response: Message | None     = None
    metadata:     dict[str, Any]     = field(default_factory=dict)


# ──────────────────────────────────────────────
# 基类
# ──────────────────────────────────────────────

class Middleware(ABC):
    """中间件基类，子类按需重写钩子方法。"""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


# ──────────────────────────────────────────────
# 中间件管理器
# ──────────────────────────────────────────────

class MiddlewareManager:
    def __init__(self, middlewares: list[Middleware] | None = None) -> None:
        self._middlewares = middlewares or []

    def add(self, middleware: Middleware) -> None:
        self._middlewares.append(middleware)

    async def execute_before_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        return await self._run_chain("before_model", ctx)

    async def execute_after_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        return await self._run_chain("after_model", ctx)

    async def execute_before_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        return await self._run_chain("before_tool", ctx)

    async def execute_after_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        return await self._run_chain("after_tool", ctx)

    async def handle_exception(self, exc: Exception, ctx: MiddlewareContext) -> bool:
        """让支持异常重试的中间件决定是否再次调用模型。"""
        for mw in self._middlewares:
            handler = getattr(mw, "handle_exception", None)
            if handler is None:
                continue
            if await handler(exc, ctx):
                return True
        return False

    async def _run_chain(self, hook: str, ctx: MiddlewareContext) -> MiddlewareResult:
        for mw in self._middlewares:
            logger.debug("中间件 %s.%s", mw.name, hook)
            result: MiddlewareResult = await getattr(mw, hook)(ctx)
            if result.action != MiddlewareAction.CONTINUE:
                logger.info("中间件 %s 阻断: action=%s", mw.name, result.action)
                return result
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


# ──────────────────────────────────────────────
# 内置中间件 1：日志
# ──────────────────────────────────────────────

class LoggingMiddleware(Middleware):
    """记录所有 LLM / 工具调用的日志。"""

    async def before_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        logger.info("[Log] before_model: iter=%d, msgs=%d", ctx.iteration, len(ctx.messages))
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        r = ctx.llm_response
        logger.info(
            "[Log] after_model: content_len=%d, tool_calls=%d",
            len(r.content) if r else 0,
            len(r.tool_calls) if r else 0,
        )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        logger.info("[Log] before_tool: %s args=%s", ctx.tool_name, ctx.tool_args)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        logger.info(
            "[Log] after_tool: %s result_len=%d",
            ctx.tool_name,
            len(ctx.tool_result) if ctx.tool_result else 0,
        )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


# ──────────────────────────────────────────────
# 内置中间件 2：重试（生产级，支持指数退避）
# ──────────────────────────────────────────────

class RetryMiddleware(Middleware):
    """自动重试失败的 LLM 调用。

    处理场景：
    - 空响应
    - 模型适配器统一转换后的限流、超时和服务不可用错误

    退避策略：wait = base_delay * (backoff_factor ** attempt)
    """

    def __init__(
        self,
        max_retries:    int   = 3,
        base_delay:     float = 1.0,
        backoff_factor: float = 2.0,
        max_delay:      float = 60.0,
    ) -> None:
        self._max_retries    = max_retries
        self._base_delay     = base_delay
        self._backoff_factor = backoff_factor
        self._max_delay      = max_delay

    def _should_retry(self, exc: Exception) -> bool:
        """判断异常是否值得重试。"""
        return isinstance(
            exc,
            (ModelRateLimitError, ModelTimeoutError, ModelUnavailableError),
        ) or bool(getattr(exc, "retryable", False))

    async def after_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        r = ctx.llm_response
        retry_count = ctx.metadata.get("retry_count", 0)

        if retry_count >= self._max_retries:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        # 空响应触发重试
        if r is not None and not r.content and not r.tool_calls:
            wait = min(
                self._base_delay * (self._backoff_factor ** retry_count),
                self._max_delay,
            )
            logger.warning(
                "[Retry] 空响应，第 %d/%d 次重试，等待 %.1fs",
                retry_count + 1, self._max_retries, wait,
            )
            await asyncio.sleep(wait)
            ctx.metadata["retry_count"] = retry_count + 1
            return MiddlewareResult(
                action=MiddlewareAction.RETRY,
                data={"retry_count": retry_count + 1},
            )

        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def handle_exception(
        self,
        exc: Exception,
        ctx: MiddlewareContext,
    ) -> bool:
        """外部调用：判断异常是否应重试，并执行等待。

        Returns:
            True 表示调用方应重试，False 表示直接抛出。
        """
        if not self._should_retry(exc):
            return False

        retry_count = ctx.metadata.get("retry_count", 0)
        if retry_count >= self._max_retries:
            return False

        wait = min(
            self._base_delay * (self._backoff_factor ** retry_count),
            self._max_delay,
        )
        logger.warning(
            "[Retry] %s，第 %d/%d 次重试，等待 %.1fs",
            type(exc).__name__, retry_count + 1, self._max_retries, wait,
        )
        await asyncio.sleep(wait)
        ctx.metadata["retry_count"] = retry_count + 1
        return True


# ──────────────────────────────────────────────
# 内置中间件 3：HITL（人机协同）
# ──────────────────────────────────────────────

class HumanInTheLoopMiddleware(Middleware):
    """人机协同：对特定工具调用暂停并等待人工审批。

    真正可用的模式：
    - auto_approve=True  : 自动批准（测试用）
    - approval_queue     : 外部注入一个可 await 的回调，返回 True/False

    生产级使用方式：
        async def my_approval(tool: str, args: dict) -> bool:
            # 推送到前端，等待用户点击确认/拒绝
            return await approval_db.wait_for_decision(tool, args)

        HumanInTheLoopMiddleware(
            approval_needed=["takeoff_drone"],
            approval_callback=my_approval,
        )
    """

    def __init__(
        self,
        approval_needed:   list[str]                                      | None = None,
        auto_approve:      bool                                                   = False,
        approval_callback: Any | None = None,  # Callable[[str, dict], Awaitable[bool]]
        timeout_seconds:   float                                                  = 300.0,
    ) -> None:
        self._needed   = set(approval_needed or [])
        self._auto     = auto_approve
        self._callback = approval_callback
        self._timeout  = timeout_seconds

    async def before_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        if ctx.tool_name not in self._needed:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        # 自动批准（测试模式）
        if self._auto:
            logger.info("[HITL] 自动批准工具: %s", ctx.tool_name)
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        # 有回调：等待人工决策
        if self._callback is not None:
            try:
                approved = await asyncio.wait_for(
                    self._callback(ctx.tool_name, ctx.tool_args),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("[HITL] 审批超时（%.0fs），拒绝工具 %s", self._timeout, ctx.tool_name)
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    data={"reason": "approval_timeout", "tool": ctx.tool_name},
                )

            if approved:
                logger.info("[HITL] 人工批准工具: %s", ctx.tool_name)
                return MiddlewareResult(action=MiddlewareAction.CONTINUE)
            else:
                logger.warning("[HITL] 人工拒绝工具: %s", ctx.tool_name)
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    data={"reason": "approval_denied", "tool": ctx.tool_name},
                )

        # 没有回调：直接 STOP（需要调用方自己处理恢复）
        logger.warning("[HITL] 工具 %s 需要审批，暂停执行", ctx.tool_name)
        return MiddlewareResult(
            action=MiddlewareAction.STOP,
            data={"reason": "approval_needed", "tool": ctx.tool_name},
        )


# ──────────────────────────────────────────────
# 内置中间件 4：Token 限制 + 摘要联动
# ──────────────────────────────────────────────

class TokenLimitMiddleware(Middleware):
    """防止消息历史超出 token 限制。

    当消息超出阈值时，自动裁剪旧消息（保留 system + 最近 N 条），
    并在 MiddlewareResult.data["messages"] 里返回裁剪后的列表，
    ReActAgent.run() 收到 MODIFY 动作后会直接使用新列表。
    """

    def __init__(
        self,
        max_tokens:      int = 4000,
        chars_per_token: int = 4,     # 粗略估算：1 token ≈ 4 字符
        keep_messages:   int = 10,    # 超限时保留最近的几条消息
    ) -> None:
        self._max_tokens      = max_tokens
        self._chars_per_token = chars_per_token
        self._keep_messages   = keep_messages

    def _estimate_tokens(self, messages: list[Message]) -> int:
        total = sum(len(m.content) for m in messages if m.content)
        return total // self._chars_per_token

    def _trim_messages(self, messages: list[Message]) -> list[Message]:
        """保留 system 消息 + 最近 keep_messages 条，丢弃中间旧消息。"""
        system_msgs = [m for m in messages if m.role == "system"]
        other_msgs  = [m for m in messages if m.role != "system"]
        kept = other_msgs[-self._keep_messages:] if len(other_msgs) > self._keep_messages else other_msgs
        return system_msgs + kept

    async def before_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        estimated = self._estimate_tokens(ctx.messages)

        if estimated <= self._max_tokens:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        trimmed = self._trim_messages(ctx.messages)
        new_estimated = self._estimate_tokens(trimmed)

        logger.warning(
            "[TokenLimit] 消息历史过长 (%d tokens)，裁剪：%d → %d 条，重新估算 %d tokens",
            estimated, len(ctx.messages), len(trimmed), new_estimated,
        )

        # 更新 ctx.messages，让 ReActAgent 使用裁剪后的版本
        ctx.messages = trimmed

        return MiddlewareResult(
            action=MiddlewareAction.MODIFY,
            data={"messages": trimmed, "trimmed_tokens": estimated - new_estimated},
        )
