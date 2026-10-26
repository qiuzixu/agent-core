"""审批队列：状态机、异步等待与 CAS 收口的核心实现。

双模式设计、三条唤醒路径与过期收口竞态的处理见 ``ApprovalQueue`` docstring。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from agent_core.access import AccessContext
from agent_core.hitl.models import ApprovalRequest, ApprovalStatus
from agent_core.ports import ApprovalStore
from agent_core.protocol.runtime import ApprovalRecord

logger = logging.getLogger(__name__)


class ApprovalQueue:
    """审批队列：进程内等待器，可选地把状态写入 ``ApprovalStore``。

    双模式设计：
    - 不配置 Store：纯进程内队列，适合测试与单实例场景；
    - 配置 Store：审批持久化，跨进程可见可处理（多实例部署必需）。

    等待 ``wait_for_decision`` 有三条唤醒路径：
    1. 同进程决定 → ``asyncio.Event`` 立即唤醒（最快）；
    2. 跨进程决定 → 按 ``poll_interval`` 轮询持久化记录；
    3. 超时兜底   → 过期 CAS 收口，以数据库终态为准。
    """

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
        # 进程内活跃请求表：同进程等待/查看的快路径（跨进程不可见）。
        self._requests: dict[str, ApprovalRequest] = {}
        # 可选持久化后端；为 None 时队列退化为纯进程内模式。
        self._approval_store = approval_store or runtime_store
        # 严格 RBAC 开关：开启后所有读写操作强制要求 AccessContext。
        self._require_access = require_access
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于 0")
        # 跨进程决定的轮询间隔：也是 Event 等待的超时切片（见 wait_for_decision）。
        self._poll_interval = poll_interval_seconds
        # 可选的通知回调：审批创建/决定后调用，供 WebSocket 推送等外部消费者使用。
        self._notify: list[Any] = []

    def add_notify_callback(self, callback: Any) -> None:
        """注册审批事件通知回调（如 WebSocket 推送）。

        回调签名: async def callback(event_type: str, data: dict) -> None
        event_type: "approval_created" | "approval_approved" | "approval_rejected"
        注册后回调失败不会影响审批流程本身。
        """
        self._notify.append(callback)

    async def _fire_notify(self, event_type: str, data: dict[str, Any]) -> None:
        """逐个触发通知回调；单个回调失败只记日志，不影响审批流程。"""
        for cb in self._notify:
            try:
                result = cb(event_type, data)
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    await result
            except Exception:
                logger.exception("审批通知回调执行失败: %s", event_type)

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
        asyncio.get_event_loop().create_task(
            self._fire_notify("approval_created", request.to_dict())
        )
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
        await self._fire_notify("approval_created", request.to_dict())
        return request

    async def wait_for_decision(
        self,
        request: ApprovalRequest,
        timeout_seconds: float = 300.0,
    ) -> ApprovalStatus:
        """阻塞等待审批决定，返回终态（APPROVED/REJECTED/TIMEOUT）。

        三条唤醒路径：同进程 Event 立即唤醒、跨进程轮询持久化记录、
        超时后过期 CAS 收口。等待期间以数据库终态为准，防止
        "deadline 刚到时决定恰好提交"的竞态被误判为超时。
        """
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
            # 主等待循环：先等同进程 Event；等不到就按 poll_interval
            # 轮询持久化记录（跨进程的决定只能从库里读到）。
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
            # 超时收口同样是"决定"，记录时间便于审计；决定人为系统（None）。
            expired.decided_at = self._now_iso()
            if await self._transition(expired):
                request.status = ApprovalStatus.TIMEOUT
                request.decided_at = expired.decided_at
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
        """批准请求：CAS 把 pending → approved，成功后唤醒同进程等待者。

        返回 False 表示请求不存在、已处理（非 pending）或 CAS 失败
        （如另一管理员刚刚处理过 / 已过期收口）。
        """
        request = await self.load_request(request_id, access=access)
        if request is None or request.status != ApprovalStatus.PENDING:
            return False
        # 审计字段：批准人取自 AccessContext，决定时间取 UTC ISO 串；
        # 配置存储时 CAS 成功后随终态一起持久化，纯内存模式只回写进程内对象。
        decided_by = access.user_id if access is not None else None
        decided_at = self._now_iso()
        if self._approval_store:
            approved = request.to_record()
            approved.status = "approved"
            approved.decided_by = decided_by
            approved.decided_at = decided_at
            if not await self._transition(approved):
                return False
        request.status = ApprovalStatus.APPROVED
        request.decided_by = decided_by
        request.decided_at = decided_at
        request._event.set()
        await self._fire_notify("approval_approved", request.to_dict())
        return True

    async def reject(
        self,
        request_id: str,
        reason: str = "用户拒绝",
        *,
        access: AccessContext | None = None,
    ) -> bool:
        """拒绝请求：CAS 把 pending → rejected 并记录原因，语义同 ``approve``。"""
        request = await self.load_request(request_id, access=access)
        if request is None or request.status != ApprovalStatus.PENDING:
            return False
        # 审计字段语义与 approve 完全一致。
        decided_by = access.user_id if access is not None else None
        decided_at = self._now_iso()
        if self._approval_store:
            rejected = request.to_record()
            rejected.status = "rejected"
            rejected.reason = reason
            rejected.decided_by = decided_by
            rejected.decided_at = decided_at
            if not await self._transition(rejected):
                return False
        request.status = ApprovalStatus.REJECTED
        request.reason = reason
        request.decided_by = decided_by
        request.decided_at = decided_at
        request._event.set()
        await self._fire_notify("approval_rejected", request.to_dict())
        return True

    async def list_pending(
        self,
        *,
        access: AccessContext | None = None,
    ) -> list[dict[str, Any]]:
        """列出可见的 pending 审批请求。

        进程内请求与持久化记录合并去重（进程内优先），
        因此其他进程/实例创建的审批也能出现在列表中。
        """
        if self._require_access and access is None:
            raise PermissionError("该 ApprovalQueue 要求提供 AccessContext")
        merged: dict[str, dict[str, Any]] = {}
        for request in self._requests.values():
            if (
                request.status is ApprovalStatus.PENDING
                and not (request.expires_at and self._is_past(request.expires_at))
                and (access is None or access.can_access(request.user_id, request.tenant_id))
            ):
                merged[request.request_id] = request.to_dict()
        if self._approval_store is not None:
            try:
                records = await self._approval_store.list_approvals(status="pending")
            except Exception:
                logger.exception("读取持久化待审批列表失败，仅返回进程内请求")
                records = []
            for record in records:
                if record.approval_id in merged:
                    continue
                if record.expires_at and self._is_past(record.expires_at):
                    # 过期的持久化记录由 load_request 访问时再做 CAS 收口。
                    continue
                if access is not None and not access.can_access(record.user_id, record.tenant_id):
                    continue
                # 统一转换为请求字典结构，与进程内条目字段保持一致。
                merged[record.approval_id] = ApprovalRequest.from_record(record).to_dict()
        return list(merged.values())

    def get_request(
        self,
        request_id: str,
        *,
        access: AccessContext | None = None,
    ) -> ApprovalRequest | None:
        """只读查看进程内请求（跨进程恢复请用 ``load_request``）。"""
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
        # 访问触发的过期收口与 wait_for_decision 保持同一审计语义。
        expired.decided_at = self._now_iso()
        if await self._transition(expired):
            request.status = ApprovalStatus.TIMEOUT
            request.decided_at = expired.decided_at
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
        if callable(transition):
            outcome = transition(record, expected_status="pending")
            if not inspect.isawaitable(outcome):
                raise TypeError("ApprovalStore.transition_approval 必须返回可等待结果")
            return bool(await outcome)
        current = await self._approval_store.load_approval(record.approval_id)
        if current is None or current.status != "pending":
            return False
        await self._approval_store.save_approval(record)
        return True

    @staticmethod
    def _apply_record(request: ApprovalRequest, record: ApprovalRecord) -> None:
        """把持久化终态回写到进程内请求，终态时点亮信号灯。"""
        restored = ApprovalRequest.from_record(record)
        request.status = restored.status
        request.reason = restored.reason
        request.expires_at = restored.expires_at
        request.decided_by = restored.decided_by
        request.decided_at = restored.decided_at
        if request.status != ApprovalStatus.PENDING:
            request._event.set()

    async def purge_terminal_approvals(self, older_than_days: float = 30.0) -> int:
        """清理超过保留期的终态审批记录，防止表无限膨胀。

        只清理 approved/rejected/expired 三种终态，pending 记录永不清理；
        返回删除的行数。未配置持久化存储时只清进程内终态请求并返回 0
        （进程内终态请求在等待结束时就已摘除，正常情况下这里无残留）。
        """
        if older_than_days <= 0:
            raise ValueError("older_than_days 必须大于 0")
        cutoff = (
            datetime.now(UTC) - timedelta(days=older_than_days)
        ).isoformat()
        for request in list(self._requests.values()):
            if (
                request.status is not ApprovalStatus.PENDING
                and request.decided_at is not None
                and request.decided_at <= cutoff
            ):
                self._requests.pop(request.request_id, None)
        if self._approval_store is None:
            return 0
        purge = getattr(self._approval_store, "purge_approvals", None)
        if not callable(purge):
            # 未实现清理接口的外部 Store：诚实跳过而不是假装清理成功。
            return 0
        # getattr 的返回值类型检查器只能推成 object，这里显式声明契约：
        # purge_approvals(cutoff) -> Awaitable[int]。
        purge_impl = cast("Callable[[str], Awaitable[int]]", purge)
        return int(await purge_impl(cutoff))

    @staticmethod
    def _now_iso() -> str:
        """当前 UTC 时间的 ISO 串（决定时间/清理基准统一格式）。"""
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _is_past(expires_at: str | None) -> bool:
        """判断 ISO 时间串是否已过期；无过期时间视为永不过期。"""
        if not expires_at:
            return False
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC)

    @staticmethod
    def _expires_at(timeout_seconds: float | None) -> str | None:
        """把相对超时（秒）换算为绝对过期时间（ISO/UTC）；None 表示永不过期。"""
        if timeout_seconds is None:
            return None
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        return (datetime.now(UTC) + timedelta(seconds=timeout_seconds)).isoformat()

    @staticmethod
    def _remaining_seconds(expires_at: str | None) -> float:
        """计算距离过期还剩多少秒；永不过期返回 inf。"""
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
        """操作/查看前校验 RBAC：未提供身份、请求无归属、非本人均拒绝。"""
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
        """创建前校验：严格 RBAC 模式下审批必须绑定用户与租户。"""
        if self._require_access and (user_id is None or tenant_id is None):
            raise PermissionError("严格访问模式要求审批绑定 user_id 和 tenant_id")
