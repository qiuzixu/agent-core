"""进程内任务存储实现：用于单元测试与单进程开发。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_core.tasks.base import AgentTaskStore
from agent_core.tasks.models import AgentTask, _now, _parse_time

class MemoryAgentTaskStore(AgentTaskStore):
    def __init__(self) -> None:
        self._tasks: dict[str, AgentTask] = {}
        self._lock = asyncio.Lock()

    async def create(self, task: AgentTask) -> AgentTask:
        async with self._lock:
            if task.idempotency_key:
                for existing in self._tasks.values():
                    if existing.idempotency_key == task.idempotency_key:
                        return existing
            self._tasks[task.task_id] = AgentTask(**task.to_dict())
            return AgentTask(**task.to_dict())

    async def get(self, task_id: str) -> AgentTask | None:
        task = self._tasks.get(task_id)
        return AgentTask(**task.to_dict()) if task else None

    async def get_by_idempotency_key(self, key: str) -> AgentTask | None:
        for task in self._tasks.values():
            if task.idempotency_key == key:
                return AgentTask(**task.to_dict())
        return None

    async def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
        attempt_id: str | None = None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            now = datetime.now(UTC)
            if task is not None and attempt_id is not None and task.attempt_id != attempt_id:
                return None
            lease_expired = (
                task is not None
                and task.status == "running"
                and (
                    task.lease_expires_at is None
                    or _parse_time(task.lease_expires_at) <= now
                )
            )
            if task is None or (
                task.status not in {"queued", "waiting_retry"} and not lease_expired
            ):
                return None
            task.status = "running"
            task.worker_id = worker_id
            task.fencing_token += 1
            task.error = None
            task.updated_at = _now()
            task.lease_expires_at = (
                now + timedelta(seconds=max(0.1, lease_seconds))
            ).isoformat()
            return AgentTask(**task.to_dict())

    async def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "running" or task.worker_id != worker_id:
                return None
            if fencing_token is not None and task.fencing_token != fencing_token:
                return None
            task.lease_expires_at = (
                datetime.now(UTC) + timedelta(seconds=max(0.1, lease_seconds))
            ).isoformat()
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def renew_leases(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> int:
        async with self._lock:
            renewed = 0
            for task in self._tasks.values():
                if task.status != "running" or task.worker_id != worker_id:
                    continue
                task.lease_expires_at = (
                    datetime.now(UTC) + timedelta(seconds=max(0.1, lease_seconds))
                ).isoformat()
                task.updated_at = _now()
                renewed += 1
            return renewed

    async def reclaim_stale(
        self,
        *,
        worker_id: str = "lease-reaper",
        limit: int = 100,
    ) -> list[AgentTask]:
        async with self._lock:
            now = datetime.now(UTC)
            reclaimed: list[AgentTask] = []
            for task in sorted(self._tasks.values(), key=lambda item: item.updated_at):
                if len(reclaimed) >= limit:
                    break
                expired = task.status == "running" and (
                    task.lease_expires_at is None
                    or _parse_time(task.lease_expires_at) <= now
                )
                if not expired:
                    continue
                task.status = "waiting_retry"
                task.worker_id = worker_id
                task.next_retry_at = now.isoformat()
                task.updated_at = _now()
                reclaimed.append(AgentTask(**task.to_dict()))
            return reclaimed

    async def complete(
        self,
        task_id: str,
        *,
        worker_id: str,
        result: dict[str, Any] | None,
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "running" or task.worker_id != worker_id:
                return None
            if fencing_token is not None and task.fencing_token != fencing_token:
                return None
            task.status = "completed"
            task.result = result
            task.error = None
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def fail(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "running" or task.worker_id != worker_id:
                return None
            if fencing_token is not None and task.fencing_token != fencing_token:
                return None
            task.retry_count += 1
            task.error = error
            task.updated_at = _now()
            if task.retry_count < task.max_retries:
                task.status = "waiting_retry"
                task.next_retry_at = (
                    datetime.now(UTC) + timedelta(seconds=max(0.1, retry_delay_seconds))
                ).isoformat()
            else:
                task.status = "failed"
                task.next_retry_at = None
            return AgentTask(**task.to_dict())

    async def mark_dead(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "running" or task.worker_id != worker_id:
                return None
            if fencing_token is not None and task.fencing_token != fencing_token:
                return None
            task.status = "dead"
            task.error = error
            task.worker_id = worker_id
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def mark_requeued(
        self,
        task_id: str,
        *,
        worker_id: str,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "dead":
                return None
            task.status = "queued"
            task.worker_id = worker_id
            task.error = None
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())


    async def mark_discarded(
        self,
        task_id: str,
        *,
        worker_id: str,
        reason: str,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "dead":
                return None
            task.status = "discarded"
            task.worker_id = worker_id
            task.error = reason
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())


    async def set_attempt(
        self,
        task_id: str,
        attempt_id: str,
        *,
        retry_count: int | None = None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status not in {"queued", "waiting_retry"}:
                return None
            task.attempt_id = attempt_id
            if retry_count is not None:
                task.retry_count = max(0, int(retry_count))
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def set_waiting_retry(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if fencing_token is not None and task.fencing_token != fencing_token:
                return None
            task.status = "waiting_retry"
            task.worker_id = worker_id
            task.error = error
            task.next_retry_at = (
                datetime.now(UTC) + timedelta(seconds=max(0.1, retry_delay_seconds))
            ).isoformat()
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def reschedule(self, task_id: str, *, worker_id: str = "scheduler") -> AgentTask | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "waiting_retry":
                return None
            task.status = "queued"
            task.worker_id = worker_id
            task.next_retry_at = None
            task.updated_at = _now()
            return AgentTask(**task.to_dict())

    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
    ) -> list[AgentTask]:
        return [
            AgentTask(**task.to_dict())
            for task in self._tasks.values()
            if task.status == status
        ][:limit]

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        intent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        worker_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        tasks = [
            AgentTask(**task.to_dict())
            for task in self._tasks.values()
            if (status is None or task.status == status)
            and (intent is None or task.intent == intent)
            and (tenant_id is None or task.tenant_id == tenant_id)
            and (user_id is None or task.user_id == user_id)
            and (worker_id is None or task.worker_id == worker_id)
        ]
        tasks.sort(key=lambda task: task.created_at)
        return tasks[:max(1, int(limit))]



__all__ = ["MemoryAgentTaskStore"]
