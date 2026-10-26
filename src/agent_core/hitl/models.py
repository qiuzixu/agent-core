"""HITL 数据模型：审批状态枚举与进程内审批请求。

状态机与并发模型的完整说明见包级 ``__init__.py``；
本模块只承载纯数据与序列化转换，不含等待与持久化逻辑。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from agent_core.protocol.runtime import ApprovalRecord


class ApprovalStatus(Enum):
    """审批请求的状态（进程内枚举；持久化时映射为字符串，见 ``to_record``）。"""

    # 等待外部决定：唯一允许转换为其他状态的状态。
    PENDING = "pending"
    # 审批人同意执行；持久化值为 "approved"。
    APPROVED = "approved"
    # 审批人拒绝执行；持久化值为 "rejected"。
    REJECTED = "rejected"
    # 超时/过期收口；持久化值为 "expired"（与 REJECTED 区分拒绝原因）。
    TIMEOUT = "timeout"


@dataclass
class ApprovalRequest:
    """一个等待外部决定的动作请求。

    进程内对象：同进程的等待者通过内藏的 ``asyncio.Event`` 被立即唤醒；
    跨进程场景则由 ``ApprovalQueue`` 轮询持久化记录来同步状态。
    """

    # 全局唯一请求 ID（uuid4），同时作为持久化记录的主键 approval_id。
    request_id: str
    # 归属的会话线程 ID，用于前端把审批关联到具体对话。
    thread_id: str
    # 待审批的工具名与参数（审批人看到的就是"是否允许执行这个工具"）。
    tool_name: str
    tool_args: dict[str, Any]
    # 触发本次审批的运行实例 ID（可选，用于追溯）。
    run_id: str = ""
    # 归属用户/租户：严格 RBAC 模式下必填，决定谁能看到/处理这条审批。
    user_id: str | None = None
    tenant_id: str | None = None
    # 当前状态；创建后进入 PENDING。
    status: ApprovalStatus = ApprovalStatus.PENDING
    # 审批人填写的决定原因（拒绝时常见）。
    reason: str | None = None
    # ISO 格式的过期时间；超过后允许任何一方做"过期收口"。
    expires_at: str | None = None
    # 审计字段：终态时记录决定人身份与决定时间（进程内回显/API 响应用）。
    decided_by: str | None = None
    decided_at: str | None = None
    # 同进程唤醒信号灯：approve/reject 成功后 set()，等待者立即醒来。
    # repr=False 避免把 Event 内部锁打进日志。
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def approval_id(self) -> str:
        """持久化主键与 request_id 同值，两个命名空间共用一个 ID。"""
        return self.request_id

    def to_record(self) -> ApprovalRecord:
        """转换为可持久化的 ``ApprovalRecord``（进程内枚举 → 存储字符串）。"""
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
            decided_by=self.decided_by,
            decided_at=self.decided_at,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为对外 JSON 结构（/approvals 返回体使用 request_id 命名）。"""
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
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
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
            decided_by=record.decided_by,
            decided_at=record.decided_at,
        )
        # 已有终态的记录恢复时直接点亮信号灯，避免等待者傻等。
        if request.status != ApprovalStatus.PENDING:
            request._event.set()
        return request
