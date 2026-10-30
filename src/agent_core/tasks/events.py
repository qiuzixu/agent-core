"""任务信封与会话事件的桥接辅助。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_core.access import AccessContext
from agent_core.storage.governance import SessionEvent

from .envelope import TaskEnvelope


def access_from_envelope(envelope: TaskEnvelope) -> AccessContext:
    """为后台执行重建调用者身份。"""
    return AccessContext(
        user_id=envelope.user_id,
        tenant_id=envelope.tenant_id,
        roles=frozenset(envelope.roles or {"user"}),
    )


def event_recorder(
    store: Any,
) -> Callable[[TaskEnvelope, str, dict[str, Any]], Awaitable[None]]:
    """返回 AgentTaskWorker 期望的事件回调。"""

    async def record(
        envelope: TaskEnvelope,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        await store.append(
            SessionEvent(
                thread_id=f"task:{envelope.task_id}",
                event_type=event_type,
                run_type="agent_task",
                run_id=envelope.task_id,
                payload={
                    "task_id": envelope.task_id,
                    "user_id": envelope.user_id,
                    "tenant_id": envelope.tenant_id,
                    **payload,
                },
            )
        )

    return record


def task_summary(task_id: str, events: list[SessionEvent]) -> dict[str, Any]:
    """从事件流派生任务摘要视图，无需单独表。"""
    event_types = [event.event_type for event in events]
    if "agent_task_failed" in event_types:
        status = "failed"
    elif "agent_task_completed" in event_types:
        status = "completed"
    elif "agent_task_started" in event_types:
        status = "running"
    else:
        status = "queued"
    return {
        "task_id": task_id,
        "status": status,
        "events": [event.to_dict() for event in events],
    }


__all__ = [
    "access_from_envelope",
    "event_recorder",
    "task_summary",
]


