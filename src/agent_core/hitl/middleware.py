"""HITL 中间件：ReAct 工具执行前的审批拦截器。"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from agent_core.hitl.models import ApprovalStatus
from agent_core.hitl.queue import ApprovalQueue
from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)
from agent_core.runtime.state import transition_run_status


class HumanInTheLoopMiddleware(Middleware):
    priority = 1
    hook_timeout = 0  # HITL 审批等待用户确认可能耗时数分钟，禁用超时隔离
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
        await self._mark_waiting_approval(context, request)
        status: ApprovalStatus | None = None
        try:
            status = await self._queue.wait_for_decision(request, timeout_seconds=self._timeout)
        finally:
            await self._clear_waiting_approval(context, request, status)
        assert status is not None
        if status == ApprovalStatus.APPROVED:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        reason = request.reason or status.value
        return MiddlewareResult(
            action=MiddlewareAction.STOP,
            data={"reason": reason, "tool": context.tool_name, "status": status.value},
        )

    async def _mark_waiting_approval(self, context: MiddlewareContext, request: Any) -> None:
        """把审批等待状态写入 RunContext，并立即持久化。"""
        runtime_context = context.run_context
        if runtime_context is None:
            return
        runtime_context.pending_approval = request.to_record()
        if runtime_context.status == "running":
            transition_run_status(runtime_context, "waiting_approval")
        runtime_context.emit(
            "run_waiting_approval",
            approval_id=request.request_id,
            tool_name=request.tool_name,
        )
        await self._persist_runtime_context(runtime_context)

    async def _clear_waiting_approval(
        self,
        context: MiddlewareContext,
        request: Any,
        status: ApprovalStatus | None,
    ) -> None:
        """审批结束后清理挂起记录并恢复运行状态。"""
        runtime_context = context.run_context
        if runtime_context is None:
            return
        runtime_context.pending_approval = None
        if runtime_context.status == "waiting_approval":
            transition_run_status(runtime_context, "running")
        runtime_context.emit(
            "run_approval_decided",
            approval_id=request.request_id,
            tool_name=request.tool_name,
            status=status.value if status is not None else "error",
        )
        await self._persist_runtime_context(runtime_context)

    @staticmethod
    async def _persist_runtime_context(runtime_context: Any) -> None:
        """通过 Runtime 注入的 checkpoint 回调保存状态。"""
        callback = runtime_context.checkpoint_callback
        if callback is None:
            return
        result = callback(runtime_context.checkpoint)
        if inspect.isawaitable(result):
            await result
