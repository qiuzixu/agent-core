"""任务存储的数据契约与解析辅助。

``AgentTask`` 是任务信封在持久化层的投影；``_now``/``_parse_time``/
``_task_from_row`` 供各存储后端共享。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)




@dataclass
class AgentTask:
    """一条 RabbitMQ Agent 任务的持久化视图。"""

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    intent: str = ""
    status: str = "queued"
    queue: str = "agent.task.normal"
    thread_id: str | None = None
    user_id: str = "anonymous"
    tenant_id: str = "default"
    idempotency_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    worker_id: str | None = None
    next_retry_at: str | None = None
    workflow_execution_id: str | None = None
    attempt_id: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    lease_expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)




def _task_from_row(value: Any) -> AgentTask:
    if not isinstance(value, (tuple, list)):
        raise TypeError("unexpected database row type")
    (
        task_id,
        intent,
        status,
        queue,
        thread_id,
        user_id,
        tenant_id,
        idempotency_key,
        payload_json,
        result_json,
        error,
        retry_count,
        max_retries,
        worker_id,
        next_retry_at,
        workflow_execution_id,
        created_at,
        updated_at,
        lease_expires_at,
        attempt_id,
    ) = value
    return AgentTask(
        task_id=str(task_id),
        intent=str(intent),
        status=str(status),
        queue=str(queue),
        thread_id=thread_id,
        user_id=str(user_id),
        tenant_id=str(tenant_id),
        idempotency_key=idempotency_key,
        payload=json.loads(payload_json or "{}"),
        result=json.loads(result_json) if result_json is not None else None,
        error=error,
        retry_count=int(retry_count),
        max_retries=int(max_retries),
        worker_id=worker_id,
        next_retry_at=next_retry_at.isoformat() if hasattr(next_retry_at, "isoformat") else next_retry_at,
        workflow_execution_id=workflow_execution_id,
        created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
        updated_at=updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at),
        lease_expires_at=(
            lease_expires_at.isoformat()
            if hasattr(lease_expires_at, "isoformat")
            else lease_expires_at
        ),
        attempt_id=attempt_id,
    )



__all__ = [
    "AgentTask",
    "_now",
    "_parse_time",
    "_task_from_row",
]
