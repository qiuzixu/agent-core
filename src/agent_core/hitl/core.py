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

from agent_core.access import AccessContext
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
            "run_id": self.run_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "status": self.status.value,
            "reason": self.reason,
        }

    @classmethod
    def from_record(cls, record: ApprovalRecord) -> "ApprovalRequest":
        """从持久化记录恢复审批请求。"""
        request = cls(
            request_id=record.approval_id,
            thread_id=record.thread_id,
            run_id=record.run_id,
            tool_name=record.action,
            tool_args=dict(record.arguments),
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            status={
                "pending": ApprovalStatus.PENDING,
                "approved": ApprovalStatus.APPROVED,
                "rejected": ApprovalStatus.REJECTED,
                "expired": ApprovalStatus.TIMEOUT,
            }.get(record.status, ApprovalStatus.PENDING),
            reason=record.reason,
        )
        if request.status != ApprovalStatus.PENDING:
            request._event.set()
        return request


class ApprovalQueue:
    """进程内等待器，可选地把状态写入 ``ApprovalStore``。"""

    def __init__(
        self,
        approval_store: ApprovalStore | None = None,
        *,
        runtime_store: ApprovalStore | None = None,
        poll_interval_seconds: float = 0.5,
        require_access: bool = False,
    ) -> None:
        # runtime_store 是旧 Vanilla 命名，保留它避免升级时破坏调用方。
        if approval_store is not None and runtime_store is not None:
            raise ValueError("approval_store 和 runtime_store 只能配置一个")
        self._requests: dict[str, ApprovalRequest] = {}
        self._approval_store = approval_store or runtime_store
        self._require_access = require_access
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")
        self._poll_interval = poll_interval_seconds

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
        self._require_owner(user_id, tenant_id)
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

    async def create_request_async(
        self,
        thread_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        run_id: str = "",
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> ApprovalRequest:
        """创建请求并等待持久化完成，适合生产执行路径。"""
        self._require_owner(user_id, tenant_id)
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
            await self._approval_store.save_approval(request.to_record())
        return request

    async def wait_for_decision(
        self,
        request: ApprovalRequest,
        timeout_seconds: float = 300.0,
    ) -> ApprovalStatus:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        try:
            while request.status == ApprovalStatus.PENDING:
                if self._approval_store is not None:
                    stored = await self._approval_store.load_approval(request.request_id)
                    if stored is not None and stored.status != "pending":
                        restored = ApprovalRequest.from_record(stored)
                        request.status = restored.status
                        request.reason = restored.reason
                        request._event.set()
                        break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        request._event.wait(),
                        timeout=min(self._poll_interval, remaining),
                    )
                except TimeoutError:
                    continue
            if request.status != ApprovalStatus.PENDING:
                return request.status
            request.status = ApprovalStatus.TIMEOUT
            return request.status
        finally:
            if self._approval_store:
                await self._approval_store.save_approval(request.to_record())
            self._requests.pop(request.request_id, None)

    async def approve(
        self,
        request_id: str,
        *,
        access: AccessContext | None = None,
    ) -> bool:
        request = await self.load_request(request_id, access=access)
        if request is None or request.status != ApprovalStatus.PENDING:
            return False
        request.status = ApprovalStatus.APPROVED
        request._event.set()
        if self._approval_store:
            await self._approval_store.save_approval(request.to_record())
        return True

    async def reject(
        self,
        request_id: str,
        reason: str = "用户拒绝",
        *,
        access: AccessContext | None = None,
    ) -> bool:
        request = await self.load_request(request_id, access=access)
        if request is None or request.status != ApprovalStatus.PENDING:
            return False
        request.status = ApprovalStatus.REJECTED
        request.reason = reason
        request._event.set()
        if self._approval_store:
            await self._approval_store.save_approval(request.to_record())
        return True

    def list_pending(
        self,
        *,
        access: AccessContext | None = None,
    ) -> list[dict[str, Any]]:
        if self._require_access and access is None:
            raise PermissionError("该 ApprovalQueue 要求提供 AccessContext")
        return [
            request.to_dict()
            for request in self._requests.values()
            if request.status == ApprovalStatus.PENDING
            and (access is None or access.can_access(request.user_id, request.tenant_id))
        ]

    def get_request(
        self,
        request_id: str,
        *,
        access: AccessContext | None = None,
    ) -> ApprovalRequest | None:
        request = self._requests.get(request_id)
        if request is not None:
            self._check_access(request, access)
        elif self._require_access and access is None:
            raise PermissionError("该 ApprovalQueue 要求提供 AccessContext")
        return request

    async def load_request(
        self,
        request_id: str,
        *,
        access: AccessContext | None = None,
    ) -> ApprovalRequest | None:
        """优先读取进程内请求，否则从持久化记录恢复。"""
        request = self._requests.get(request_id)
        if request is not None:
            self._check_access(request, access)
            return request
        if self._require_access and access is None:
            raise PermissionError("该 ApprovalQueue 要求提供 AccessContext")
        if self._approval_store is None:
            return None
        record = await self._approval_store.load_approval(request_id)
        if record is None:
            return None
        request = ApprovalRequest.from_record(record)
        self._check_access(request, access)
        self._requests[request_id] = request
        return request

    def _check_access(
        self,
        request: ApprovalRequest,
        access: AccessContext | None,
    ) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 ApprovalQueue 要求提供 AccessContext")
        if self._require_access and (
            request.user_id is None or request.tenant_id is None
        ):
            raise PermissionError("该审批请求尚未绑定用户和租户")
        if access is not None and not access.can_access(request.user_id, request.tenant_id):
            raise PermissionError("无权处理该审批请求")

    def _require_owner(
        self,
        user_id: str | None,
        tenant_id: str | None,
    ) -> None:
        if self._require_access and (user_id is None or tenant_id is None):
            raise PermissionError("严格访问模式要求审批绑定 user_id 和 tenant_id")


class HumanInTheLoopMiddleware(Middleware):
    """在工具执行前暂停，等待审批队列返回决定。"""

    def __init__(
        self,
        queue: ApprovalQueue | None = None,
        approval_needed: list[str] | None = None,
        thread_id: str = "default",
        auto_approve: bool = False,
        approval_callback: Any | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._queue = queue or ApprovalQueue()
        self._needed = set(approval_needed or ())
        self._thread = thread_id
        self._auto = auto_approve
        self._callback = approval_callback
        self._timeout = timeout_seconds

    async def before_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        if ctx.tool_name not in self._needed or self._auto:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        if self._callback is not None:
            try:
                approved = await asyncio.wait_for(
                    self._callback(ctx.tool_name, ctx.tool_args),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    data={"reason": "approval_timeout", "tool": ctx.tool_name},
                )
            if approved:
                return MiddlewareResult(action=MiddlewareAction.CONTINUE)
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                data={"reason": "approval_denied", "tool": ctx.tool_name},
            )
        request = await self._queue.create_request_async(
            thread_id=str(ctx.metadata.get("thread_id") or self._thread),
            tool_name=ctx.tool_name or "unknown",
            tool_args=dict(ctx.tool_args),
            run_id=str(ctx.metadata.get("run_id") or ""),
            user_id=ctx.metadata.get("user_id"),
            tenant_id=ctx.metadata.get("tenant_id"),
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
