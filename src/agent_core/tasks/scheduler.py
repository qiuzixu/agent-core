"""尽力而为的调度器：将滞留在 waiting_retry 的持久化任务重新入队。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from .envelope import TaskEnvelope
from .metrics import default_task_metrics
from .queue import TaskQueue
from .store import AgentTaskStore

logger = logging.getLogger(__name__)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class AgentTaskRetryScheduler:
    """重新入队到期的任务；RabbitMQ 重试仍是快路径。"""

    def __init__(
        self,
        *,
        queue: TaskQueue,
        task_store: AgentTaskStore,
        worker_id: str = "task-retry-scheduler",
        batch_size: int = 100,
        dlq_reconcile_enabled: bool = False,
        dlq_reconcile_interval_seconds: float = 60.0,
    ) -> None:
        self._queue = queue
        self._task_store = task_store
        self._worker_id = worker_id
        self._batch_size = max(1, int(batch_size))
        self._dlq_reconcile_enabled = bool(dlq_reconcile_enabled)
        self._dlq_reconcile_interval_seconds = max(5.0, float(dlq_reconcile_interval_seconds))
        self._task: asyncio.Task[None] | None = None
        self._dlq_task: asyncio.Task[None] | None = None

    async def resume_due_retries(self, *, now: datetime | None = None) -> int:
        """将到期的 waiting_retry 任务移回普通队列。"""
        current = now or datetime.now(UTC)
        # 回收租约到期仍未完成的 Worker（崩溃恢复）。
        await self._task_store.reclaim_stale(worker_id=self._worker_id)
        candidates = await self._task_store.list_by_status(
            "waiting_retry",
            limit=self._batch_size,
        )
        resumed = 0
        for task in candidates:
            due_at = _parse_time(task.next_retry_at)
            if due_at is None or due_at > current:
                continue
            queued = await self._task_store.reschedule(task.task_id, worker_id=self._worker_id)
            if queued is None:
                continue
            next_attempt_id = str(uuid4())
            scheduled = await self._task_store.set_attempt(task.task_id, next_attempt_id)
            if scheduled is None:
                continue
            envelope = TaskEnvelope(
                task_id=task.task_id,
                intent=task.intent,
                thread_id=task.thread_id,
                user_id=task.user_id,
                tenant_id=task.tenant_id,
                idempotency_key=task.idempotency_key,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                requested_at=task.created_at,
                attempt_id=next_attempt_id,
                payload=task.payload,
            )
            try:
                await self._queue.publish(envelope)
                default_task_metrics.inc("task_retry_scheduled_total")
                resumed += 1
            except Exception as exc:
                await self._task_store.set_waiting_retry(
                    task.task_id,
                    worker_id=self._worker_id,
                    error=f"retry scheduler publish failed: {exc}",
                    retry_delay_seconds=5.0,
                )
                raise
        return resumed

    async def reconcile_dlq(self, *, limit: int = 100) -> dict[str, int]:
        """确认逻辑状态不再是 dead 的物理 DLQ 消息。"""
        # reconcile_dlq 是 RabbitMQ 队列的可选能力，内存队列没有；用 getattr 诚实探测。
        reconcile = getattr(self._queue, "reconcile_dlq", None)
        if not self._dlq_reconcile_enabled or not callable(reconcile):
            return {"acked": 0, "remaining": 0}

        async def should_ack(envelope: TaskEnvelope) -> bool:
            task = await self._task_store.get(envelope.task_id)
            return task is None or task.status != "dead"

        # getattr 返回值只能推成 object，这里显式声明 reconcile 的调用契约。
        reconcile_impl = cast(
            "Callable[..., Awaitable[tuple[int, int]]]",
            reconcile,
        )
        acked, remaining = await reconcile_impl(
            should_ack,
            limit=max(1, min(int(limit), 500)),
        )
        if acked:
            default_task_metrics.inc("dlq_reconciled_total", acked)
            logger.info("agent task DLQ reconciled", extra={"acked": acked, "remaining": remaining})
        return {"acked": acked, "remaining": remaining}

    async def _loop(self, interval_seconds: float) -> None:
        while True:
            try:
                await self.resume_due_retries()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("agent task retry scheduler failed")
            await asyncio.sleep(max(1.0, interval_seconds))

    async def _dlq_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(max(1.0, interval_seconds))
            try:
                await self.reconcile_dlq()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("agent task DLQ reconciliation failed")

    async def start(self, *, interval_seconds: float = 30.0) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._task = asyncio.create_task(
            self._loop(max(1.0, interval_seconds)),
            name="agent-task-retry-scheduler",
        )
        if self._dlq_reconcile_enabled and self._dlq_task is None:
            self._dlq_task = asyncio.create_task(
                self._dlq_loop(self._dlq_reconcile_interval_seconds),
                name="agent-task-dlq-reconciler",
            )
        return self._task

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        dlq_task = self._dlq_task
        if dlq_task is not None:
            dlq_task.cancel()
            try:
                await dlq_task
            except asyncio.CancelledError:
                pass
            self._dlq_task = None


__all__ = ["AgentTaskRetryScheduler"]



