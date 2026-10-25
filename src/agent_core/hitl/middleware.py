"""HITL 中间件：ReAct 工具执行前的审批拦截器。"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_core.hitl.models import ApprovalStatus
from agent_core.hitl.queue import ApprovalQueue
from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)


class HumanInTheLoopMiddleware(Middleware):
    """在工具执行前暂停，等待审批队列返回决定。

    三种工作模式（按优先级）：
    1. 工具不在 ``approval_needed`` 名单或 ``auto_approve=True`` → 直接放行；
    2. 配置 ``approval_callback`` → 由自定义回调决定（老模式）；
    3. 默认 → 走 ``ApprovalQueue`` 完整流程（创建请求 + 等待决定）。
    """

    def __init__(
        self,
        queue: ApprovalQueue | None = None,
        approval_needed: list[str] | None = None,
        thread_id: str = "default",
        auto_approve: bool = False,
        approval_callback: Any | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        # 未注入队列时退化为无持久化的独立队列（仅适合测试）。
        self._queue = queue or ApprovalQueue()
        # 需要审批的工具名单；空集 = 全部放行。
        self._needed = set(approval_needed or ())
        # 请求未带 thread_id 时的兜底值。
        self._thread = thread_id
        # True 时全部自动放行（调试用）。
        self._auto = auto_approve
        # 自定义决定回调（老模式，优先于队列流程）。
        self._callback = approval_callback
        # 等待决定的最长秒数，超时按 STOP 收口。
        self._timeout = timeout_seconds

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        """工具执行前拦截：需要审批的工具会挂起等待决定。"""
        # 模式 1：名单外或自动放行 → 直接继续执行。
        if context.tool_name not in self._needed or self._auto:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 模式 2：自定义回调决定（超时按拒绝处理）。
        if self._callback is not None:
            try:
                approved = await asyncio.wait_for(
                    self._callback(context.tool_name, context.tool_args),
                    timeout=self._timeout,
                )
            except TimeoutError:
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    data={"reason": "approval_timeout", "tool": context.tool_name},
                )
            if approved:
                return MiddlewareResult(action=MiddlewareAction.CONTINUE)
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                data={"reason": "approval_denied", "tool": context.tool_name},
            )
        # 模式 3：走完整审批流程 —— 创建请求（含持久化）→ 阻塞等待 → 决定。
        request = await self._queue.create_request_async(
            thread_id=str(context.metadata.get("thread_id") or self._thread),
            tool_name=context.tool_name or "unknown",
            tool_args=dict(context.tool_args),
            run_id=str(context.metadata.get("run_id") or ""),
            user_id=context.metadata.get("user_id"),
            tenant_id=context.metadata.get("tenant_id"),
            timeout_seconds=self._timeout,
        )
        # 把请求 ID 写入上下文，便于工具执行记录追溯本次审批。
        context.metadata["approval_request_id"] = request.request_id
        status = await self._queue.wait_for_decision(request, timeout_seconds=self._timeout)
        if status == ApprovalStatus.APPROVED:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        reason = request.reason or status.value
        return MiddlewareResult(
            action=MiddlewareAction.STOP,
            data={"reason": reason, "tool": context.tool_name, "status": status.value},
        )
