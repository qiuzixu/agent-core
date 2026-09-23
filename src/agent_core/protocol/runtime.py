"""Agent 运行时共享协议。

这些类型是 ReAct、业务编排器、工作流和 HTTP runtime 之间的公共边界，
避免每一层用不同的字典字段表示同一个运行状态。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


RunStatus = Literal[
    "queued", "running", "waiting_approval", "waiting_clarification",
    "interrupted", "completed", "failed", "cancelled",
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunEvent:
    """一次运行中的不可变事件。"""

    event_type: str
    run_id: str
    thread_id: str
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "sequence": self.sequence,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }


@dataclass
class ApprovalRecord:
    """可持久化的人机审批记录。"""

    thread_id: str
    run_id: str
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None
    tenant_id: str | None = None
    approval_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    reason: str | None = None
    created_at: str = field(default_factory=_now)
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "action": self.action,
            "arguments": self.arguments,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass
class ToolResult:
    """工具执行的结构化结果。"""

    tool_name: str
    success: bool
    value: Any = None
    error: str | None = None
    error_kind: Literal[
        "", "not_found", "invalid_arguments", "timeout", "approval", "execution"
    ] = ""
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        """转换成兼容旧 ReAct 消息的文本。"""
        if self.success:
            return str(self.value) if self.value is not None else ""
        return f"[工具错误:{self.error_kind or 'execution'}] {self.error or '未知错误'}"

    def to_dict(self) -> dict[str, Any]:
        value = self.value
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
        return {
            "tool_name": self.tool_name,
            "success": self.success,
            "value": value,
            "error": self.error,
            "error_kind": self.error_kind,
            "retryable": self.retryable,
            "metadata": self.metadata,
        }


@dataclass
class RunContext:
    """一次 Agent run 的统一可恢复上下文。"""

    thread_id: str
    run_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    status: RunStatus = "queued"
    version: int = 0
    idempotency_key: str | None = None
    iteration: int = 0
    tool_calls_used: int = 0
    pending_approval: ApprovalRecord | None = None
    pending_clarification: dict[str, Any] | None = None
    state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    # 最后一个可恢复的 Agent Loop 快照；回调只存在于进程内，不参与序列化。
    checkpoint: dict[str, Any] = field(default_factory=dict)
    checkpoint_callback: Any = field(default=None, repr=False, compare=False)
    events: list[RunEvent] = field(default_factory=list)

    def emit(self, event_type: str, **payload: Any) -> RunEvent:
        """追加有序事件，供 API、审计和调试读取。"""
        event = RunEvent(
            event_type=event_type,
            run_id=self.run_id,
            thread_id=self.thread_id,
            sequence=len(self.events),
            payload=payload,
        )
        self.events.append(event)
        return event

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "status": self.status,
            "version": self.version,
            "idempotency_key": self.idempotency_key,
            "iteration": self.iteration,
            "tool_calls_used": self.tool_calls_used,
            "pending_approval": (
                self.pending_approval.to_dict() if self.pending_approval else None
            ),
            "pending_clarification": self.pending_clarification,
            "state": self.state,
            "metadata": self.metadata,
            "checkpoint": self.checkpoint,
            "events": [event.to_dict() for event in self.events],
        }
