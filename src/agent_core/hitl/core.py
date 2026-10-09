"""通用人机协同审批队列。

Core 只负责审批状态机和异步等待，不负责 HTTP 路由、认证或具体业务动作。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Literal

from agent_core.access import AccessContext
from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)
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
    expires_at: str | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def approval_id(self) -> str:
        return self.request_id

    def to_record(self) -> ApprovalRecord:
        status_by_request: dict[
            ApprovalStatus,
            Literal["pending", "approved", "rejected", "expired"],
        ] = {
            ApprovalStatus.PENDING: "pending",
            ApprovalStatus.APPROVED: "approved",
            ApprovalStatus.REJECTED: "rejected",
            ApprovalStatus.TIMEOUT: "expired",
        }
        return ApprovalRecord(
            approval_id=self.request_id,
            thread_id=self.thread_id,
            run_id=self.run_id,
            action=self.tool_name,
            arguments=dict(self.tool_args),
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            status=status_by_request[self.status],
            reason=self.reason,
            expires_at=self.expires_at,
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
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_record(cls, record: ApprovalRecord) -> ApprovalRequest:
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
            expires_at=record.expires_at,
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
        timeout_seconds: float | None = None,
    ) -> ApprovalRequest:
        """创建仅驻留当前进程的审批请求。

        配置持久化存储后必须使用 ``create_request_async``，确保方法返回时初始审批
        已写入存储，避免进程退出或后台任务失败造成记录丢失。
        """
        self._require_owner(user_id, tenant_id)
        if self._approval_store is not None:
            raise RuntimeError("配置审批存储后必须使用 await create_request_async()")
        request = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            thread_id=thread_id,
            tool_name=tool_name,
            tool_args=dict(tool_args),
            run_id=run_id,
            user_id=user_id,
            tenant_id=tenant_id,
            expires_at=self._expires_at(timeout_seconds),
        )
        self._requests[request.request_id] = request
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
        timeout_seconds: float | None = None,
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
            expires_at=self._expires_at(timeout_seconds),
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
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        loop = asyncio.get_running_loop()
        if request.expires_at is None:
            request.expires_at = self._expires_at(timeout_seconds)
        remaining_lifetime = self._remaining_seconds(request.expires_at)
        deadline = loop.time() + min(timeout_seconds, max(0.0, remaining_lifetime))
        if self._approval_store is not None:
            # pending -> pending 也是条件更新，用于持久化 deadline，不能覆盖已经完成的决定。
            updated = await self._transition(request.to_record())
            if not updated:
                stored = await self._approval_store.load_approval(request.request_id)
                if stored is not None:
                    self._apply_record(request, stored)
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
            if self._approval_store is None:
                request.status = ApprovalStatus.TIMEOUT
                return request.status

            expired = request.to_record()
            expired.status = "expired"
            if await self._transition(expired):
                request.status = ApprovalStatus.TIMEOUT
                request._event.set()
                return request.status

            # 决定可能刚好在 deadline 到达时提交；以持久化终态为准。
            stored = await self._approval_store.load_approval(request.request_id)
            if stored is not None:
                self._apply_record(request, stored)
                return request.status
            # 记录不存在（幻影请求）：不可能再有决定提交，按超时收口而不是返回 PENDING。
            request.status = ApprovalStatus.TIMEOUT
            request._event.set()
            return request.status
        finally:
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
        if self._approval_store:
            approved = request.to_record()
            approved.status = "approved"
            if not await self._transition(approved):
                return False
        request.status = ApprovalStatus.APPROVED
        request._event.set()
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
        if self._approval_store:
            rejected = request.to_record()
            rejected.status = "rejected"
            rejected.reason = reason
            if not await self._transition(rejected):
                return False
        request.status = ApprovalStatus.REJECTED
        request.reason = reason
        request._event.set()
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
            and not (request.expires_at and self._is_past(request.expires_at))
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
            await self._expire_if_due(request)
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
        await self._expire_if_due(request)
        self._check_access(request, access)
        self._requests[request_id] = request
        return request

    async def _expire_if_due(self, request: ApprovalRequest) -> None:
        """对已过期的 pending 请求执行过期收口；配置存储时以 CAS 结果为准。"""
        if request.status is not ApprovalStatus.PENDING or not request.expires_at:
            return
        if not self._is_past(request.expires_at):
            return
        if self._approval_store is None:
            request.status = ApprovalStatus.TIMEOUT
            request._event.set()
            return
        expired = request.to_record()
        expired.status = "expired"
        if await self._transition(expired):
            request.status = ApprovalStatus.TIMEOUT
            request._event.set()
        else:
            latest = await self._approval_store.load_approval(request.request_id)
            if latest is not None:
                self._apply_record(request, latest)

    async def _transition(self, record: ApprovalRecord) -> bool:
        """执行审批 CAS；兼容尚未升级条件更新接口的外部 Store。

        内置 Memory/SQLite/PostgreSQL Store 的 ``transition_approval`` 是原子条件更新。
        未实现该接口的外部 Store 会退化为 load→check→save，跨进程并发下存在
        last-writer-wins 窗口；生产环境应让外部 Store 实现 ``transition_approval``。
        """
        if self._approval_store is None:
            return False
        transition = getattr(self._approval_store, "transition_approval", None)
        if transition is not None:
            return bool(await transition(record, expected_status="pending"))
        current = await self._approval_store.load_approval(record.approval_id)
        if current is None or current.status != "pending":
            return False
        await self._approval_store.save_approval(record)
        return True

    @staticmethod
    def _apply_record(request: ApprovalRequest, record: ApprovalRecord) -> None:
        restored = ApprovalRequest.from_record(record)
        request.status = restored.status
        request.reason = restored.reason
        request.expires_at = restored.expires_at
        if request.status != ApprovalStatus.PENDING:
            request._event.set()

    @staticmethod
    def _record_expired(record: ApprovalRecord) -> bool:
        return ApprovalQueue._is_past(record.expires_at)

    @staticmethod
    def _is_past(expires_at: str | None) -> bool:
        if not expires_at:
            return False
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC)

    @staticmethod
    def _expires_at(timeout_seconds: float | None) -> str | None:
        if timeout_seconds is None:
            return None
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        return (datetime.now(UTC) + timedelta(seconds=timeout_seconds)).isoformat()

    @staticmethod
    def _remaining_seconds(expires_at: str | None) -> float:
        if expires_at is None:
            return float("inf")
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return (parsed - datetime.now(UTC)).total_seconds()

    def _check_access(
        self,
        request: ApprovalRequest,
        access: AccessContext | None,
    ) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 ApprovalQueue 要求提供 AccessContext")
        if self._require_access and (request.user_id is None or request.tenant_id is None):
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
            except TimeoutError:
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
            timeout_seconds=self._timeout,
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
