"""通用人机协同审批队列。

Core 只负责审批状态机和异步等待，不负责 HTTP 路由、认证或具体业务动作。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_core.middleware import Middleware, MiddlewareAction, MiddlewareContext, MiddlewareResult
from agent_core.ports import ApprovalStore
from agent_core.protocol.runtime import ApprovalRecord

logger = logging.getLogger(__name__)


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


@dataclass
class ApprovalRequest:
    """一个等待外部决定的动作请求。"""

    request_id: str
    thread_id: str
    tool_name: str
    tool_args: dict[str, Any]
    run_id: str = ""
    user_id: str | None = None
    tenant_id: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def approval_id(self) -> str:
        return self.request_id

    def to_record(self) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=self.request_id,
            thread_id=self.thread_id,
            run_id=self.run_id,
            action=self.tool_name,
            arguments=dict(self.tool_args),
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            status={
                ApprovalStatus.PENDING: "pending",
                ApprovalStatus.APPROVED: "approved",
                ApprovalStatus.REJECTED: "rejected",
                ApprovalStatus.TIMEOUT: "expired",
            }[self.status],
            reason=self.reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "thread_id": self.thread_id,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "status": self.status.value,
            "reason": self.reason,
        }


class ApprovalQueue:
    """进程内等待器，可选地把状态写入 ``ApprovalStore``。"""

    def __init__(
        self,
        approval_store: ApprovalStore | None = None,
        *,
        runtime_store: ApprovalStore | None = None,
    ) -> None:
        # runtime_store 是旧 Vanilla 命名，保留它避免升级时破坏调用方。
        if approval_store is not None and runtime_store is not None:
            raise ValueError("approval_store 和 runtime_store 只能配置一个")
        self._requests: dict[str, ApprovalRequest] = {}
        self._approval_store = approval_store or runtime_store

    def create_request(
        self,
        thread_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        run_id: str = "",
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            thread_id=thread_id,
            tool_name=tool_name,
            tool_args=dict(tool_args),
            run_id=run_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        self._requests[request.request_id] = request
        if self._approval_store:
            asyncio.create_task(self._approval_store.save_approval(request.to_record()))
        return request

    async def wait_for_decision(
        self,
        request: ApprovalRequest,
        timeout_seconds: float = 300.0,
    ) -> ApprovalStatus:
        try:
            await asyncio.wait_for(request._event.wait(), timeout=timeout_seconds)
            return request.status
        except asyncio.TimeoutError:
            request.status = ApprovalStatus.TIMEOUT
            return request.status
        finally:
            if self._approval_store:
                await self._approval_store.save_approval(request.to_record())
            self._requests.pop(request.request_id, None)

    async def approve(self, request_id: str) -> bool:
        request = self._requests.get(request_id)
        if request is None or request.status != ApprovalStatus.PENDING:
            return False
        request.status = ApprovalStatus.APPROVED
        request._event.set()
        if self._approval_store:
            await self._approval_store.save_approval(request.to_record())
        return True

    async def reject(self, request_id: str, reason: str = "用户拒绝") -> bool:
        request = self._requests.get(request_id)
        if request is None or request.status != ApprovalStatus.PENDING:
            return False
        request.status = ApprovalStatus.REJECTED
        request.reason = reason
        request._event.set()
        if self._approval_store:
            await self._approval_store.save_approval(request.to_record())
        return True

    def list_pending(self) -> list[dict[str, Any]]:
        return [
            request.to_dict()
            for request in self._requests.values()
            if request.status == ApprovalStatus.PENDING
        ]

    def get_request(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)


class HumanInTheLoopMiddleware(Middleware):
    """在工具执行前暂停，等待审批队列返回决定。"""

    def __init__(
        self,
        queue: ApprovalQueue | None = None,
        approval_needed: list[str] = (),
        thread_id: str = "default",
        auto_approve: bool = False,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._queue = queue or ApprovalQueue()
        self._needed = set(approval_needed)
        self._thread = thread_id
        self._auto = auto_approve
        self._timeout = timeout_seconds

    async def before_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        if ctx.tool_name not in self._needed or self._auto:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        request = self._queue.create_request(
            thread_id=self._thread,
            tool_name=ctx.tool_name or "unknown",
            tool_args=dict(ctx.tool_args),
        )
        ctx.metadata["approval_request_id"] = request.request_id
        status = await self._queue.wait_for_decision(request, timeout_seconds=self._timeout)
        if status == ApprovalStatus.APPROVED:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        reason = request.reason or status.value
        return MiddlewareResult(
            action=MiddlewareAction.STOP,
            data={"reason": reason, "tool": ctx.tool_name, "status": status.value},
        )


__all__ = [
    "ApprovalQueue",
    "ApprovalRequest",
    "ApprovalStatus",
    "HumanInTheLoopMiddleware",
]
