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
import inspect

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_core.errors import (
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from agent_core.protocol.messages import Message

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 基础数据类型
# ──────────────────────────────────────────────


class MiddlewareAction(Enum):
    CONTINUE = "continue"  # 继续执行
    STOP = "stop"  # 停止执行（中止整个循环）
    MODIFY = "modify"  # 修改数据（如裁剪消息历史）
    RETRY = "retry"  # 重试当前 LLM 调用
    ERROR = "error"  # 返回错误


@dataclass
class MiddlewareResult:
    action: MiddlewareAction
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class MiddlewareContext:
    messages: list[Message] = field(default_factory=list)
    iteration: int = 0
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: str | None = None
    llm_response: Message | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # 运行时事件上报入口。它让通用中间件可以记录压缩、重试等事件，
    # 同时保持对独立单元测试和旧 Vanilla 调用方的兼容。
    emit: Callable[..., Any] | None = None


# ──────────────────────────────────────────────
# 基类
# ──────────────────────────────────────────────


class Middleware:
    """中间件基类，子类按需重写钩子方法。

    优先级（priority）：数值越小越先执行。MiddlewareManager 构造时按
    priority 升序排列，同一优先级内保持插入顺序（稳定排序）。

    推荐分层：
        安全类  1~9    （HITL、Guardrails、Auth、InjectionGuard、RBAC）
        治理类  10~19  （熔断器、幂等、成本预算、超时预算、工具校验）
        性能类  20~29  （Compaction、TokenLimit、SemanticCache、ToolCache）
        可观测  30~39  （Trace、Audit、Alerting、Feedback）
        兜底类  40+    （Fallback、DLQ）
        默认值  100    （未声明优先级的自定义中间件排最后）

    注意：Compaction 必须排在 TokenLimit 之前，否则硬截断会抢在
    智能压缩前面执行，摘要永远不会生成。

    超时隔离（hook_timeout）：单个中间件钩子调用的最大等待秒数。
    超时后该中间件的返回值视为 CONTINUE，链继续走下一个中间件，
    防止某个中间件卡死阻塞整条管道。设为 0 或负数表示不隔离。
    安全类中间件（如 HITL 审批等待用户确认）可能天然耗时较长，
    应设置更大的超时值或设为 0 禁用超时。
    """

    priority: int = 100
    hook_timeout: float = 30.0

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
    def __init__(
        self,
        middlewares: list[Middleware] | None = None,
        *,
        default_hook_timeout: float = 30.0,
    ) -> None:
        # 按优先级升序排列：priority 小的先执行，同一优先级保持插入顺序
        self._middlewares = sorted(middlewares or [], key=lambda mw: getattr(mw, "priority", 100))
        # 每个中间件钩子调用的默认超时上限（秒）；中间件可通过 hook_timeout 覆盖
        self._default_hook_timeout = default_hook_timeout

    def add(self, middleware: Middleware) -> None:
        self._middlewares.append(middleware)
        self._middlewares.sort(key=lambda mw: getattr(mw, "priority", 100))

    def describe_chain(self) -> list[dict[str, Any]]:
        """返回当前中间件链的诊断信息（按执行顺序），用于排查拦截问题。"""
        return [
            {"name": mw.name, "priority": getattr(mw, "priority", 100)}
            for mw in self._middlewares
        ]

    async def execute_before_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        return await self._run_chain("before_model", ctx)

    async def execute_after_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        return await self._run_chain("after_model", ctx)

    async def execute_before_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        return await self._run_chain("before_tool", ctx)

    async def execute_after_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        return await self._run_chain("after_tool", ctx)

    async def handle_exception(self, exc: Exception, ctx: MiddlewareContext) -> bool:
        """让支持异常重试的中间件决定是否再次调用模型。

        同样带超时隔离：异常处理超时的中间件视为不接管（返回 False），
        链继续问下一个中间件。
        """
        for mw in self._middlewares:
            handler = getattr(mw, "handle_exception", None)
            if handler is None:
                continue
            outcome = handler(exc, ctx)
            if not inspect.isawaitable(outcome):
                raise TypeError("Middleware.handle_exception 必须返回可等待结果")
            timeout = getattr(mw, "hook_timeout", 0) or self._default_hook_timeout
            if timeout and timeout > 0:
                try:
                    should_retry = await asyncio.wait_for(outcome, timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[Middleware] %s.handle_exception 超时（%.1f 秒），跳过",
                        mw.name, timeout,
                    )
                    continue
            else:
                should_retry = await outcome
            if should_retry:
                return True
        return False

    async def _run_chain(self, hook: str, ctx: MiddlewareContext) -> MiddlewareResult:
        modified_data: dict[str, Any] = {}
        for mw in self._middlewares:
            logger.debug("中间件 %s.%s", mw.name, hook)
            previous_messages = ctx.messages
            hook_call = getattr(mw, hook, None)
            if not callable(hook_call):
                raise TypeError(f"Middleware 未实现 {hook}()")
            hook_outcome = hook_call(ctx)
            if not inspect.isawaitable(hook_outcome):
                raise TypeError(f"Middleware.{hook} 必须返回可等待结果")

            # 超时隔离：单个中间件卡死不阻塞整条链。
            # 超时后视为 CONTINUE 继续走下一个中间件，并记录告警日志。
            timeout = getattr(mw, "hook_timeout", 0) or self._default_hook_timeout
            if timeout and timeout > 0:
                try:
                    result: MiddlewareResult = await asyncio.wait_for(hook_outcome, timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "[Middleware] %s.%s 超时（%.1f 秒），跳过该中间件继续执行链",
                        mw.name, hook, timeout,
                    )
                    continue
            else:
                result = await hook_outcome
            if result.action == MiddlewareAction.MODIFY:
                # MODIFY 有两种合法写法：替换 ctx 字段（推荐），或在 data 中携带修改。
                # 此处对账：本轮替换了 ctx.messages 时以 ctx 为准，防止 data 携带的
                # 同名旧值把 ctx 的修改覆盖掉（Agent Loop 只读取聚合后的 data）。
                if result.data:
                    modified_data.update(result.data)
                if ctx.messages is not previous_messages:
                    modified_data["messages"] = ctx.messages
                continue
            if result.action != MiddlewareAction.CONTINUE:
                logger.info("中间件 %s 阻断: action=%s", mw.name, result.action)
                return result
        if modified_data:
            return MiddlewareResult(action=MiddlewareAction.MODIFY, data=modified_data)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


# ──────────────────────────────────────────────
# 内置中间件 1：日志
# ──────────────────────────────────────────────


class LoggingMiddleware(Middleware):
    priority = 50
    """记录所有 LLM / 工具调用的日志。"""

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        logger.info("[Log] before_model: iter=%d, msgs=%d", context.iteration, len(context.messages))
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        r = context.llm_response
        logger.info(
            "[Log] after_model: content_len=%d, tool_calls=%d",
            len(r.content) if r else 0,
            len(r.tool_calls) if r else 0,
        )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        logger.info("[Log] before_tool: %s args=%s", context.tool_name, context.tool_args)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        logger.info(
            "[Log] after_tool: %s result_len=%d",
            context.tool_name,
            len(context.tool_result) if context.tool_result else 0,
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
        max_retries: int = 3,
        base_delay: float = 1.0,
        backoff_factor: float = 2.0,
        max_delay: float = 60.0,
    ) -> None:
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._backoff_factor = backoff_factor
        self._max_delay = max_delay

    def _should_retry(self, exc: Exception) -> bool:
        """判断异常是否值得重试。"""
        return isinstance(
            exc,
            (ModelRateLimitError, ModelTimeoutError, ModelUnavailableError),
        ) or bool(getattr(exc, "retryable", False))

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        r = context.llm_response
        retry_count = context.metadata.get("retry_count", 0)

        if retry_count >= self._max_retries:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        # 空响应触发重试
        if r is not None and not r.content and not r.tool_calls:
            wait = min(
                self._base_delay * (self._backoff_factor**retry_count),
                self._max_delay,
            )
            logger.warning(
                "[Retry] 空响应，第 %d/%d 次重试，等待 %.1fs",
                retry_count + 1,
                self._max_retries,
                wait,
            )
            await asyncio.sleep(wait)
            context.metadata["retry_count"] = retry_count + 1
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
            self._base_delay * (self._backoff_factor**retry_count),
            self._max_delay,
        )
        logger.warning(
            "[Retry] %s，第 %d/%d 次重试，等待 %.1fs",
            type(exc).__name__,
            retry_count + 1,
            self._max_retries,
            wait,
        )
        await asyncio.sleep(wait)
        ctx.metadata["retry_count"] = retry_count + 1
        return True
