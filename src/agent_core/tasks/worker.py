"""后台 Worker：消费任务信封并发出 started/completed/failed 会话事件。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from .envelope import TaskEnvelope
from .metrics import TaskMetrics, default_task_metrics
from .queue import TaskQueue
from .registry import PermanentTaskError, TaskLeaseLostError, TaskTimeoutError
from .store import AgentTaskStore

logger = logging.getLogger(__name__)

TaskResult = dict[str, Any] | None


class AgentTaskWorker:
    """消费任务信封并发出统一的会话事件。"""

    def __init__(
        self,
        queue: TaskQueue,
        handler: Callable[[TaskEnvelope], Awaitable[TaskResult]],
        *,
        event_recorder: Callable[[TaskEnvelope, str, dict[str, Any]], Awaitable[None]] | None = None,
        task_store: AgentTaskStore | None = None,
        worker_id: str = "agent-task-worker",
        retry_delay_seconds: float = 5.0,
        lease_seconds: float = 30.0,
        heartbeat_interval_seconds: float = 10.0,
        timeout_seconds: float | None = None,
        timeout_seconds_by_intent: Mapping[str, float] | None = None,
        concurrency: int = 10,
        concurrency_by_intent: Mapping[str, int] | None = None,
        metrics: TaskMetrics | None = None,
    ) -> None:
        self._queue = queue
        self._handler = handler
        self._event_recorder = event_recorder
        self._task_store = task_store
        self._worker_id = worker_id
        self._retry_delay_seconds = retry_delay_seconds
        self._lease_seconds = max(1.0, float(lease_seconds))
        self._heartbeat_interval_seconds = max(1.0, float(heartbeat_interval_seconds))
        self._timeout_seconds = timeout_seconds
        self._timeout_seconds_by_intent = dict(timeout_seconds_by_intent or {})
        self._default_concurrency = max(1, int(concurrency))
        self._concurrency_by_intent = {
            intent: max(1, int(value))
            for intent, value in dict(concurrency_by_intent or {}).items()
        }
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._metrics = metrics or default_task_metrics
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._active_leases: dict[str, int] = {}
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._heartbeat_failures: dict[str, int] = {}
        self._lease_lost: set[str] = set()

    async def start(self) -> None:
        if self._task_store is not None:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(),
                name=f"{self._worker_id}-heartbeat",
            )
        configure_concurrency = getattr(self._queue, "set_concurrency", None)
        if callable(configure_concurrency):
            configure_concurrency(self._default_concurrency)
        try:
            await self._queue.consume(self._process)
        finally:
            await self._stop_heartbeat()

    async def _stop_heartbeat(self) -> None:
        task = self._heartbeat_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        if self._task_store is None:
            return
        while True:
            for task_id, fencing_token in tuple(self._active_leases.items()):
                try:
                    renewed = await self._task_store.heartbeat(
                        task_id,
                        worker_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                        fencing_token=fencing_token,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures = self._heartbeat_failures.get(task_id, 0) + 1
                    self._heartbeat_failures[task_id] = failures
                    if failures < 2:
                        logger.warning("任务租约续期暂时失败：task_id=%s error=%s", task_id, exc)
                        continue
                    renewed = None
                if renewed is None:
                    self._lease_lost.add(task_id)
                    active = self._active_tasks.get(task_id)
                    if active is not None and not active.done():
                        active.cancel()
                else:
                    self._heartbeat_failures.pop(task_id, None)
            await asyncio.sleep(self._heartbeat_interval_seconds)

    def _semaphore_for(self, intent: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(intent)
        if semaphore is None:
            limit = self._concurrency_by_intent.get(intent, self._default_concurrency)
            semaphore = asyncio.Semaphore(max(1, int(limit)))
            self._semaphores[intent] = semaphore
        return semaphore

    def _log(
        self,
        level: int,
        message: str,
        envelope: TaskEnvelope,
        **fields: Any,
    ) -> None:
        logger.log(
            level,
            message,
            extra={
                "task_id": envelope.task_id,
                "attempt_id": envelope.attempt_id,
                "intent": envelope.intent,
                "tenant_id": envelope.tenant_id,
                "worker_id": self._worker_id,
                **fields,
            },
        )

    async def _process(self, envelope: TaskEnvelope) -> TaskResult:
        semaphore = self._semaphore_for(envelope.intent)
        async with semaphore:
            return await self._execute(envelope)

    async def _execute(self, envelope: TaskEnvelope) -> TaskResult:
        """认领任务并执行；租约丢失时禁止旧 Worker 提交结果。"""
        fencing_token: int | None = None
        if self._task_store is not None:
            claimed = await self._task_store.claim(
                envelope.task_id,
                self._worker_id,
                lease_seconds=self._lease_seconds,
                attempt_id=envelope.attempt_id,
            )
            if claimed is None:
                existing = await self._task_store.get(envelope.task_id)
                if existing is not None and existing.status == "completed":
                    return existing.result
                return {"status": "already_claimed", "task_id": envelope.task_id}
            fencing_token = claimed.fencing_token
            self._active_leases[envelope.task_id] = fencing_token
            current_task = asyncio.current_task()
            if current_task is not None:
                self._active_tasks[envelope.task_id] = current_task
        try:
            return await self._execute_claimed(envelope, fencing_token=fencing_token)
        finally:
            self._active_leases.pop(envelope.task_id, None)
            self._active_tasks.pop(envelope.task_id, None)
            self._heartbeat_failures.pop(envelope.task_id, None)
            self._lease_lost.discard(envelope.task_id)

    async def _execute_claimed(
        self,
        envelope: TaskEnvelope,
        *,
        fencing_token: int | None,
    ) -> TaskResult:
        """执行已经认领的任务。"""
        self._metrics.inc("task_started_total")
        started_at = time.monotonic()
        self._log(logging.INFO, "agent task started", envelope)
        await self._emit(envelope, "agent_task_started", {"intent": envelope.intent})
        timeout = self._timeout_seconds_by_intent.get(envelope.intent, self._timeout_seconds)
        try:
            if timeout is not None:
                result = await asyncio.wait_for(
                    self._handler(envelope),
                    timeout=max(0.1, float(timeout)),
                )
            else:
                result = await self._handler(envelope)
        except asyncio.CancelledError as exc:
            if envelope.task_id in self._lease_lost:
                raise TaskLeaseLostError(f"任务 {envelope.task_id} 的 Worker 租约已丢失") from exc
            raise
        except TimeoutError as exc:
            error = f"任务执行超过 {timeout} 秒"
            if self._task_store is not None:
                failed = await self._task_store.fail(
                    envelope.task_id,
                    worker_id=self._worker_id,
                    error=error,
                    retry_delay_seconds=self._retry_delay_seconds,
                    fencing_token=fencing_token,
                )
                if failed is None:
                    raise TaskLeaseLostError(f"任务 {envelope.task_id} 的 Worker 租约已丢失") from exc
                next_attempt_id = str(uuid4())
                await self._task_store.set_attempt(envelope.task_id, next_attempt_id)
                envelope.attempt_id = next_attempt_id
            self._metrics.inc("task_failed_total")
            self._log(logging.WARNING, "任务执行超时", envelope, error=error)
            await self._emit(
                envelope,
                "agent_task_failed",
                {"intent": envelope.intent, "error": error, "retry_count": envelope.retry_count},
            )
            raise TaskTimeoutError(error) from exc
        except Exception as exc:
            if self._task_store is not None:
                if isinstance(exc, TaskLeaseLostError):
                    raise
                if isinstance(exc, PermanentTaskError):
                    updated = await self._task_store.mark_dead(
                        envelope.task_id,
                        worker_id=self._worker_id,
                        error=str(exc),
                        fencing_token=fencing_token,
                    )
                else:
                    updated = await self._task_store.fail(
                        envelope.task_id,
                        worker_id=self._worker_id,
                        error=str(exc),
                        retry_delay_seconds=self._retry_delay_seconds,
                        fencing_token=fencing_token,
                    )
                if updated is None:
                    raise TaskLeaseLostError(f"任务 {envelope.task_id} 的 Worker 租约已丢失") from exc
                if not isinstance(exc, PermanentTaskError):
                    next_attempt_id = str(uuid4())
                    await self._task_store.set_attempt(envelope.task_id, next_attempt_id)
                    envelope.attempt_id = next_attempt_id
            self._metrics.inc("task_failed_total")
            self._log(logging.WARNING, "任务执行失败", envelope, error=str(exc))
            await self._emit(
                envelope,
                "agent_task_failed",
                {"intent": envelope.intent, "error": str(exc), "retry_count": envelope.retry_count},
            )
            raise
        if self._task_store is not None:
            completed = await self._task_store.complete(
                envelope.task_id,
                worker_id=self._worker_id,
                result=result,
                fencing_token=fencing_token,
            )
            if completed is None:
                raise TaskLeaseLostError(f"任务 {envelope.task_id} 的 Worker 租约已丢失")
        self._metrics.inc("task_completed_total")
        self._log(logging.INFO, "任务执行完成", envelope)
        await self._emit(envelope, "agent_task_completed", {"intent": envelope.intent, "result": result})
        return result

    async def _emit(
        self,
        envelope: TaskEnvelope,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if self._event_recorder is None:
            return
        await self._event_recorder(
            envelope,
            event_type,
            payload,
        )


