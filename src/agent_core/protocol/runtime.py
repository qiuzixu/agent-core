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
    "queued",
    "running",
    "waiting_approval",
    "waiting_clarification",
    "interrupted",
    "completed",
    "failed",
    "cancelled",
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunEvent:
    """一次运行中的生命周期事件。

    CallbackManager、EventSink、WebSocket 和审计存储消费同一个事件对象，避免多套事件协议。
    """

    event_type: str
    run_id: str
    thread_id: str
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_run_id: str | None = None
    component: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "sequence": self.sequence,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
            "parent_run_id": self.parent_run_id,
            "component": self.component,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "usage": dict(self.usage),
            "error": self.error,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunEvent:
        return cls(
            event_type=str(value["event_type"]),
            run_id=str(value["run_id"]),
            thread_id=str(value["thread_id"]),
            sequence=int(value["sequence"]),
            payload=dict(value.get("payload") or {}),
            timestamp=str(value.get("timestamp") or _now()),
            event_id=str(value.get("event_id") or uuid.uuid4()),
            parent_run_id=value.get("parent_run_id"),
            component=value.get("component"),
            tags=[str(tag) for tag in value.get("tags", [])],
            metadata=dict(value.get("metadata") or {}),
            usage=dict(value.get("usage") or {}),
            error=value.get("error"),
            duration_ms=(float(value["duration_ms"]) if value.get("duration_ms") is not None else None),
        )


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

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ApprovalRecord:
        return cls(
            approval_id=str(value.get("approval_id", "")),
            thread_id=str(value.get("thread_id", "")),
            run_id=str(value.get("run_id", "")),
            action=str(value.get("action", "")),
            arguments=dict(value.get("arguments") or {}),
            user_id=value.get("user_id"),
            tenant_id=value.get("tenant_id"),
            status=value.get("status", "pending"),
            reason=value.get("reason"),
            created_at=str(value.get("created_at") or _now()),
            expires_at=value.get("expires_at"),
        )


@dataclass
class ToolResult:
    """工具执行的结构化结果。"""

    tool_name: str
    success: bool
    value: Any = None
    error: str | None = None
    error_kind: Literal["", "not_found", "invalid_arguments", "timeout", "approval", "execution"] = ""
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

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ToolResult:
        return cls(
            tool_name=str(value["tool_name"]),
            success=bool(value.get("success", False)),
            value=value.get("value"),
            error=value.get("error"),
            error_kind=value.get("error_kind", ""),
            retryable=bool(value.get("retryable", False)),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass
class RunContext:
    """一次 Agent run 的统一可恢复上下文。"""

    thread_id: str
    run_id: str
    user_id: str | None = None
    tenant_id: str | None = None
    parent_run_id: str | None = None
    tags: list[str] = field(default_factory=list)
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

    def emit(
        self,
        event_type: str,
        *,
        component: str | None = None,
        event_metadata: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: float | None = None,
        **payload: Any,
    ) -> RunEvent:
        """追加有序事件，供 API、审计和调试读取。"""

        inferred_component = component or event_type.partition("_")[0] or None
        if error is not None:
            # 兼容旧消费者读取 payload["error"]，同时提供统一顶层字段。
            payload.setdefault("error", error)
        event = RunEvent(
            event_type=event_type,
            run_id=self.run_id,
            thread_id=self.thread_id,
            sequence=len(self.events),
            payload=payload,
            parent_run_id=self.parent_run_id,
            component=inferred_component,
            tags=list(self.tags),
            metadata=dict(event_metadata or {}),
            usage=dict(usage or {}),
            error=error,
            duration_ms=duration_ms,
        )
        self.events.append(event)
        return event

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "parent_run_id": self.parent_run_id,
            "tags": list(self.tags),
            "status": self.status,
            "version": self.version,
            "idempotency_key": self.idempotency_key,
            "iteration": self.iteration,
            "tool_calls_used": self.tool_calls_used,
            "pending_approval": (self.pending_approval.to_dict() if self.pending_approval else None),
            "pending_clarification": self.pending_clarification,
            "state": self.state,
            "metadata": self.metadata,
            "checkpoint": self.checkpoint,
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunContext:
        """从持久化字典恢复上下文；进程内回调不会参与恢复。"""

        pending_approval = value.get("pending_approval")
        context = cls(
            thread_id=str(value["thread_id"]),
            run_id=str(value["run_id"]),
            user_id=value.get("user_id"),
            tenant_id=value.get("tenant_id"),
            parent_run_id=value.get("parent_run_id"),
            tags=[str(tag) for tag in value.get("tags", [])],
            status=value.get("status", "queued"),
            version=int(value.get("version", 0)),
            idempotency_key=value.get("idempotency_key"),
            iteration=int(value.get("iteration", 0)),
            tool_calls_used=int(value.get("tool_calls_used", 0)),
            pending_approval=(
                ApprovalRecord.from_dict(pending_approval) if isinstance(pending_approval, dict) else None
            ),
            pending_clarification=value.get("pending_clarification"),
            state=dict(value.get("state") or {}),
            metadata=dict(value.get("metadata") or {}),
            checkpoint=dict(value.get("checkpoint") or {}),
        )
        context.events = [
            RunEvent.from_dict(item) for item in value.get("events", []) if isinstance(item, dict)
        ]
        return context
