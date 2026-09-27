"""多 Agent 编排的可持久化值对象。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from agent_core import AccessContext, RunEvent

AgentResultStatus = Literal["completed", "failed", "cancelled", "interrupted"]
CoordinationStatus = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不能为空")
    return normalized


@dataclass(frozen=True)
class AgentDescriptor:
    """一个可被 Supervisor 发现和调用的 Agent。"""

    agent_id: str
    description: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    aliases: frozenset[str] = field(default_factory=frozenset)
    required_roles: frozenset[str] = field(default_factory=frozenset)
    priority: int = 0
    max_concurrency: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_id", _non_empty(self.agent_id, "agent_id"))
        object.__setattr__(self, "description", _non_empty(self.description, "description"))
        object.__setattr__(
            self,
            "capabilities",
            frozenset(_non_empty(value, "capability") for value in self.capabilities),
        )
        object.__setattr__(
            self,
            "aliases",
            frozenset(_non_empty(value, "alias") for value in self.aliases),
        )
        object.__setattr__(
            self,
            "required_roles",
            frozenset(_non_empty(value, "required_role") for value in self.required_roles),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.agent_id in self.aliases:
            raise ValueError("aliases 不需要重复 agent_id")
        if self.max_concurrency is not None and self.max_concurrency <= 0:
            raise ValueError("max_concurrency 必须大于 0")

    def can_invoke(self, access: AccessContext | None) -> bool:
        if not self.required_roles:
            return True
        return access is not None and self.required_roles.issubset(access.roles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "description": self.description,
            "capabilities": sorted(self.capabilities),
            "aliases": sorted(self.aliases),
            "required_roles": sorted(self.required_roles),
            "priority": self.priority,
            "max_concurrency": self.max_concurrency,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class HandoffRequest:
    """当前 Agent 请求把任务交给另一个 Agent。"""

    target_agent_id: str | None = None
    required_capability: str | None = None
    input: str | None = None
    reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        target = self.target_agent_id.strip() if self.target_agent_id else None
        capability = self.required_capability.strip() if self.required_capability else None
        if not target and not capability:
            raise ValueError("handoff 必须指定 target_agent_id 或 required_capability")
        object.__setattr__(self, "target_agent_id", target)
        object.__setattr__(self, "required_capability", capability)
        object.__setattr__(self, "context", dict(self.context))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_agent_id": self.target_agent_id,
            "required_capability": self.required_capability,
            "input": self.input,
            "reason": self.reason,
            "context": dict(self.context),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> HandoffRequest:
        return cls(
            target_agent_id=value.get("target_agent_id"),
            required_capability=value.get("required_capability"),
            input=value.get("input"),
            reason=str(value.get("reason", "")),
            context=dict(value.get("context") or {}),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class AgentTask:
    """Supervisor 交给某个 Agent 的不可变任务。"""

    input: str
    thread_id: str
    target_agent_id: str | None = None
    required_capability: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    access: AccessContext | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    root_task_id: str | None = None
    parent_task_id: str | None = None
    parent_run_id: str | None = None
    coordination_id: str | None = None
    idempotency_key: str | None = None
    resume_requested: bool = False
    depth: int = 0
    lineage: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "input", _non_empty(self.input, "input"))
        object.__setattr__(self, "thread_id", _non_empty(self.thread_id, "thread_id"))
        object.__setattr__(self, "task_id", _non_empty(self.task_id, "task_id"))
        object.__setattr__(self, "root_task_id", self.root_task_id or self.task_id)
        object.__setattr__(self, "context", dict(self.context))
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(self, "lineage", tuple(self.lineage))
        if self.depth < 0:
            raise ValueError("depth 不能小于 0")
        if self.target_agent_id is not None and not self.target_agent_id.strip():
            raise ValueError("target_agent_id 不能为空字符串")
        if self.required_capability is not None and not self.required_capability.strip():
            raise ValueError("required_capability 不能为空字符串")
        if self.target_agent_id is not None:
            object.__setattr__(self, "target_agent_id", self.target_agent_id.strip())
        if self.required_capability is not None:
            object.__setattr__(self, "required_capability", self.required_capability.strip())

    def bind_execution(self, execution_id: str) -> AgentTask:
        """把根任务绑定到协调实例，并生成稳定的子调用幂等键。"""
        return replace(
            self,
            coordination_id=execution_id,
            idempotency_key=self.idempotency_key or f"multi-agent:{execution_id}:{self.task_id}",
        )

    def handoff(
        self,
        request: HandoffRequest,
        *,
        current_agent_id: str,
        parent_run_id: str | None,
        fallback_input: str,
    ) -> AgentTask:
        """从 handoff 请求构造下一个子任务。"""
        task_id = str(uuid.uuid4())
        context = {**self.context, **request.context}
        metadata = {**self.metadata, **request.metadata, "handoff_reason": request.reason}
        return AgentTask(
            task_id=task_id,
            root_task_id=self.root_task_id,
            parent_task_id=self.task_id,
            parent_run_id=parent_run_id or self.parent_run_id,
            coordination_id=self.coordination_id,
            thread_id=self.thread_id,
            input=request.input or fallback_input,
            target_agent_id=request.target_agent_id,
            required_capability=request.required_capability,
            context=context,
            access=self.access,
            metadata=metadata,
            idempotency_key=f"multi-agent:{self.coordination_id}:{task_id}",
            depth=self.depth + 1,
            lineage=(*self.lineage, current_agent_id),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "root_task_id": self.root_task_id,
            "parent_task_id": self.parent_task_id,
            "parent_run_id": self.parent_run_id,
            "coordination_id": self.coordination_id,
            "thread_id": self.thread_id,
            "input": self.input,
            "target_agent_id": self.target_agent_id,
            "required_capability": self.required_capability,
            "context": dict(self.context),
            "access": self.access.to_dict() if self.access else None,
            "metadata": dict(self.metadata),
            "idempotency_key": self.idempotency_key,
            "resume_requested": self.resume_requested,
            "depth": self.depth,
            "lineage": list(self.lineage),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentTask:
        access_value = value.get("access")
        access = None
        if isinstance(access_value, dict):
            access = AccessContext(
                user_id=str(access_value.get("user_id", "anonymous")),
                tenant_id=str(access_value.get("tenant_id", "default")),
                roles=frozenset(str(role) for role in access_value.get("roles", ["user"])),
            )
        return cls(
            task_id=str(value["task_id"]),
            root_task_id=value.get("root_task_id"),
            parent_task_id=value.get("parent_task_id"),
            parent_run_id=value.get("parent_run_id"),
            coordination_id=value.get("coordination_id"),
            thread_id=str(value["thread_id"]),
            input=str(value["input"]),
            target_agent_id=value.get("target_agent_id"),
            required_capability=value.get("required_capability"),
            context=dict(value.get("context") or {}),
            access=access,
            metadata=dict(value.get("metadata") or {}),
            idempotency_key=value.get("idempotency_key"),
            resume_requested=bool(value.get("resume_requested", False)),
            depth=int(value.get("depth", 0)),
            lineage=tuple(str(item) for item in value.get("lineage", [])),
        )


@dataclass(frozen=True)
class AgentResult:
    """子 Agent 的统一执行结果。"""

    task_id: str
    agent_id: str
    status: AgentResultStatus
    output: str = ""
    run_id: str | None = None
    handoff: HandoffRequest | None = None
    error: str | None = None
    retryable: bool = False
    usage: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _non_empty(self.task_id, "task_id"))
        object.__setattr__(self, "agent_id", _non_empty(self.agent_id, "agent_id"))
        object.__setattr__(self, "usage", {str(key): float(value) for key, value in self.usage.items()})
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.status not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError(f"无效 AgentResult status：{self.status}")
        if self.handoff is not None and self.status != "completed":
            raise ValueError("只有 completed 结果可以携带 handoff")
        if self.status == "failed" and not self.error:
            raise ValueError("failed 结果必须提供 error")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "status": self.status,
            "output": self.output,
            "run_id": self.run_id,
            "handoff": self.handoff.to_dict() if self.handoff else None,
            "error": self.error,
            "retryable": self.retryable,
            "usage": dict(self.usage),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AgentResult:
        handoff = value.get("handoff")
        return cls(
            task_id=str(value["task_id"]),
            agent_id=str(value["agent_id"]),
            status=cast(AgentResultStatus, value["status"]),
            output=str(value.get("output", "")),
            run_id=value.get("run_id"),
            handoff=HandoffRequest.from_dict(handoff) if isinstance(handoff, dict) else None,
            error=value.get("error"),
            retryable=bool(value.get("retryable", False)),
            usage={str(key): float(item) for key, item in dict(value.get("usage") or {}).items()},
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass
class CoordinationExecution:
    """可在每个 Agent 边界保存和恢复的协调实例。"""

    root_task: AgentTask
    current_task: AgentTask | None
    execution_id: str
    status: CoordinationStatus = "queued"
    current_agent_id: str | None = None
    results: list[AgentResult] = field(default_factory=list)
    route_history: list[str] = field(default_factory=list)
    events: list[RunEvent] = field(default_factory=list)
    output: str = ""
    error: str | None = None
    usage: dict[str, float] = field(default_factory=dict)
    handoffs_completed: int = 0
    agent_calls: int = 0
    version: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        self.execution_id = _non_empty(self.execution_id, "execution_id")
        if self.status not in {"queued", "running", "completed", "failed", "cancelled", "interrupted"}:
            raise ValueError(f"无效 CoordinationExecution status：{self.status}")

    @property
    def access(self) -> AccessContext | None:
        return self.root_task.access

    def emit(self, event_type: str, **payload: Any) -> RunEvent:
        error_value = payload.get("error")
        usage_value = payload.get("usage")
        event = RunEvent(
            event_type=event_type,
            run_id=self.execution_id,
            thread_id=self.root_task.thread_id,
            sequence=len(self.events),
            payload=payload,
            parent_run_id=self.root_task.parent_run_id,
            component="multi_agent",
            tags=["multi-agent"],
            usage=dict(usage_value) if isinstance(usage_value, dict) else {},
            error=str(error_value) if error_value is not None else None,
        )
        self.events.append(event)
        return event

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "root_task": self.root_task.to_dict(),
            "current_task": self.current_task.to_dict() if self.current_task else None,
            "status": self.status,
            "current_agent_id": self.current_agent_id,
            "results": [result.to_dict() for result in self.results],
            "route_history": list(self.route_history),
            "events": [event.to_dict() for event in self.events],
            "output": self.output,
            "error": self.error,
            "usage": dict(self.usage),
            "handoffs_completed": self.handoffs_completed,
            "agent_calls": self.agent_calls,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CoordinationExecution:
        current = value.get("current_task")
        return cls(
            execution_id=str(value["execution_id"]),
            root_task=AgentTask.from_dict(dict(value["root_task"])),
            current_task=AgentTask.from_dict(current) if isinstance(current, dict) else None,
            status=cast(CoordinationStatus, value.get("status", "queued")),
            current_agent_id=value.get("current_agent_id"),
            results=[AgentResult.from_dict(item) for item in value.get("results", [])],
            route_history=[str(item) for item in value.get("route_history", [])],
            events=[RunEvent.from_dict(item) for item in value.get("events", [])],
            output=str(value.get("output", "")),
            error=value.get("error"),
            usage={str(key): float(item) for key, item in dict(value.get("usage") or {}).items()},
            handoffs_completed=int(value.get("handoffs_completed", 0)),
            agent_calls=int(value.get("agent_calls", 0)),
            version=int(value.get("version", 0)),
            created_at=str(value.get("created_at") or _now()),
            updated_at=str(value.get("updated_at") or _now()),
        )


@dataclass(frozen=True)
class RoutingDecision:
    """一次可审计的路由结果。"""

    agent_id: str
    reason: str


@dataclass(frozen=True)
class CoordinationPolicy:
    """Supervisor 的调用、恢复和资源预算。"""

    max_handoffs: int = 8
    max_agent_calls: int = 16
    max_visits_per_agent: int = 2
    max_agent_attempts: int = 1
    agent_timeout_seconds: float = 120.0
    retry_base_seconds: float = 0.5
    max_parallelism: int = 4
    max_total_tokens: float | None = None
    max_total_cost: float | None = None
    require_access: bool = False
    lease_seconds: float = 30.0
    heartbeat_seconds: float = 10.0

    def __post_init__(self) -> None:
        positive_ints = {
            "max_agent_calls": self.max_agent_calls,
            "max_visits_per_agent": self.max_visits_per_agent,
            "max_agent_attempts": self.max_agent_attempts,
            "max_parallelism": self.max_parallelism,
        }
        if self.max_handoffs < 0:
            raise ValueError("max_handoffs 不能小于 0")
        for name, value in positive_ints.items():
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.agent_timeout_seconds <= 0:
            raise ValueError("agent_timeout_seconds 必须大于 0")
        if self.retry_base_seconds < 0:
            raise ValueError("retry_base_seconds 不能小于 0")
        if self.max_total_tokens is not None and self.max_total_tokens <= 0:
            raise ValueError("max_total_tokens 必须大于 0")
        if self.max_total_cost is not None and self.max_total_cost <= 0:
            raise ValueError("max_total_cost 必须大于 0")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        if self.heartbeat_seconds <= 0 or self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat_seconds 必须大于 0 且小于 lease_seconds")
