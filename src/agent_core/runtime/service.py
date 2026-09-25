"""通用 Agent Runtime 生命周期服务。

Runtime 负责运行实例和生命周期，业务编排仍由调用方注入到工具或上层服务中。
这样 API 层不需要重复实现 start、resume、cancel 和事件发布逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from typing import Any

from agent_core.access import AccessContext
from agent_core.ports import EventSink, RunStore
from agent_core.protocol.runtime import RunContext, RunEvent
from agent_core.runtime.react import ReActAgent
from agent_core.runtime.store import MemoryRunStore
from agent_core.storage.lease import RunLeaseStore
from agent_core.storage.runtime import RuntimeConcurrencyError

logger = logging.getLogger(__name__)


class MemoryEventSink(EventSink):
    """测试和开发环境使用的事件接收器。"""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def publish(self, event: RunEvent) -> None:
        self.events.append(event)


@dataclass
class RuntimeRun:
    """运行句柄，供需要后台任务的 API 层使用。"""

    context: RunContext
    task: asyncio.Task[str] | None = None


class AgentRuntime:
    """围绕 ``ReActAgent`` 的通用生命周期门面。"""

    def __init__(
        self,
        agent: ReActAgent,
        *,
        run_store: RunStore | None = None,
        event_sink: EventSink | None = None,
        lease_store: RunLeaseStore | None = None,
        worker_id: str | None = None,
        lease_seconds: float = 30.0,
        heartbeat_seconds: float = 10.0,
        require_access: bool = False,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds 必须大于 0 且小于 lease_seconds")
        self._agent = agent
        self._run_store = run_store or MemoryRunStore()
        self._event_sink = event_sink
        self._lease_store = lease_store
        self._worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._require_access = require_access
        self._tasks: dict[str, asyncio.Task[str]] = {}
        self._event_cursors: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        thread_id: str,
        user_input: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
        idempotency_key: str | None = None,
        memory_context: str | None = None,
        metadata: dict[str, Any] | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> RuntimeRun:
        """创建后台运行实例并立即返回句柄。"""
        self._require_owner(user_id, tenant_id)
        async with self._lock:
            if idempotency_key:
                existing = await self._run_store.find_run_by_idempotency(
                    tenant_id, user_id, idempotency_key
                )
                if existing is not None:
                    task = self._tasks.get(existing.run_id)
                    return RuntimeRun(context=existing, task=task)

            context = RunContext(
                thread_id=thread_id,
                run_id=str(uuid.uuid4()),
                user_id=user_id,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                metadata={
                    **(metadata or {}),
                    "_input": user_input,
                },
                checkpoint=checkpoint_state or {},
            )
            await self._run_store.save_run(context)
            await self._claim(context)
            task = asyncio.create_task(
                self._run_with_lease(
                    context,
                    self._execute(
                        context,
                        user_input,
                        memory_context=memory_context,
                        checkpoint_state=checkpoint_state,
                    ),
                )
            )
            self._tasks[context.run_id] = task
            task.add_done_callback(lambda _: self._tasks.pop(context.run_id, None))
            return RuntimeRun(context=context, task=task)

    async def run(
        self,
        thread_id: str,
        user_input: str,
        **kwargs: Any,
    ) -> RunContext:
        """同步等待一次完整运行并返回最终上下文。"""
        handle = await self.start(thread_id, user_input, **kwargs)
        access = self._context_access(handle.context)
        await self.wait(
            handle.context.thread_id,
            handle.context.run_id,
            access=access,
        )
        return await self.get(
            handle.context.thread_id,
            handle.context.run_id,
            access=access,
        )

    async def wait(
        self,
        thread_id: str,
        run_id: str,
        *,
        access: AccessContext | None = None,
    ) -> RunContext:
        """等待进程内任务；进程重启后可读取持久化状态。"""
        context = await self._run_store.load_run(thread_id, run_id)
        if context is None:
            raise KeyError(f"run 不存在：{run_id}")
        self._check_access(context, access)
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)
        context = await self._run_store.load_run(thread_id, run_id)
        if context is None:
            raise KeyError(f"run 不存在：{run_id}")
        self._check_access(context, access)
        return context

    async def get(
        self,
        thread_id: str,
        run_id: str,
        *,
        access: AccessContext | None = None,
    ) -> RunContext:
        """读取运行上下文。"""
        context = await self._run_store.load_run(thread_id, run_id)
        if context is None:
            raise KeyError(f"run 不存在：{run_id}")
        self._check_access(context, access)
        return context

    async def resume(
        self,
        thread_id: str,
        run_id: str,
        *,
        access: AccessContext | None = None,
    ) -> RuntimeRun:
        """从已有 checkpoint 继续运行同一个 run。"""
        async with self._lock:
            if run_id in self._tasks:
                return RuntimeRun(
                    context=await self.get(thread_id, run_id, access=access),
                    task=self._tasks[run_id],
                )
            context = await self.get(thread_id, run_id, access=access)
            if not context.checkpoint:
                raise ValueError("该 run 没有可恢复的 checkpoint")
            await self._claim(context)
            try:
                context.status = "queued"
                context.emit("run_resumed")
                await self._run_store.save_run(context)
            except Exception:
                await self._release(context)
                raise
            task = asyncio.create_task(
                self._run_with_lease(
                    context,
                    self._execute(
                        context,
                        "",
                        checkpoint_state=context.checkpoint,
                    ),
                )
            )
            self._tasks[run_id] = task
            task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
            return RuntimeRun(context=context, task=task)

    async def cancel(
        self,
        thread_id: str,
        run_id: str,
        *,
        access: AccessContext | None = None,
    ) -> bool:
        """取消进程内运行；已完成或只存在于数据库中的 run 不会被伪造修改。"""
        context = await self.get(thread_id, run_id, access=access)
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        context = await self.get(thread_id, run_id, access=access)
        return context.status == "cancelled"

    async def stream(
        self,
        thread_id: str,
        user_input: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """以流式方式执行一次运行，并复用同一生命周期持久化逻辑。"""
        context = RunContext(
            thread_id=thread_id,
            run_id=str(uuid.uuid4()),
            user_id=kwargs.pop("user_id", None),
            tenant_id=kwargs.pop("tenant_id", None),
            idempotency_key=kwargs.pop("idempotency_key", None),
            metadata={**kwargs.pop("metadata", {}), "_input": user_input},
        )
        self._require_owner(context.user_id, context.tenant_id)
        await self._run_store.save_run(context)
        await self._claim(context)
        context.status = "running"
        context.emit("run_started", input=user_input)
        context.checkpoint_callback = self._checkpoint_callback(context)
        lease_errors: list[BaseException] = []
        heartbeat = self._start_stream_heartbeat(context, lease_errors)
        try:
            async for chunk in self._agent.stream(
                user_input,
                memory_context=kwargs.pop("memory_context", None),
                thread_id=thread_id,
                run_context=context,
                checkpoint_state=kwargs.pop("checkpoint_state", None),
            ):
                yield chunk
        except asyncio.CancelledError:
            if not lease_errors:
                raise
            context.status = "interrupted"
            context.emit(
                "run_interrupted",
                reason="worker_lease_lost",
                error=str(lease_errors[0]),
            )
            raise RuntimeConcurrencyError(
                f"run {context.run_id} 的 Worker 租约已丢失"
            ) from lease_errors[0]
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await self._release(context)
            await self._persist(context)

    async def recover_expired_runs(self, *, auto_resume: bool = False) -> list[RunContext]:
        """标记租约过期的 Run，并可从 checkpoint 自动恢复。"""
        if self._lease_store is None:
            return []
        recovered: list[RunContext] = []
        for lease in await self._lease_store.list_expired():
            context = await self._run_store.load_run(lease.thread_id, lease.run_id)
            await self._lease_store.release(lease.thread_id, lease.run_id, lease.worker_id)
            if context is None or context.status not in {"queued", "running"}:
                continue
            context.status = "interrupted"
            context.emit("run_interrupted", reason="worker_lease_expired")
            await self._run_store.save_run(context)
            recovered.append(context)
            if auto_resume and context.checkpoint:
                await self.resume(
                    context.thread_id,
                    context.run_id,
                    access=self._context_access(context),
                )
        return recovered

    async def _claim(self, context: RunContext) -> None:
        if self._lease_store is None:
            return
        claimed = await self._lease_store.claim(
            context.thread_id,
            context.run_id,
            self._worker_id,
            ttl_seconds=self._lease_seconds,
        )
        if not claimed:
            raise RuntimeConcurrencyError(f"run {context.run_id} 已被其他 Worker 认领")

    async def _release(self, context: RunContext) -> None:
        if self._lease_store is not None:
            await self._lease_store.release(
                context.thread_id,
                context.run_id,
                self._worker_id,
            )

    async def _heartbeat(self, context: RunContext) -> None:
        assert self._lease_store is not None
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            renewed = await self._lease_store.renew(
                context.thread_id,
                context.run_id,
                self._worker_id,
                ttl_seconds=self._lease_seconds,
            )
            if not renewed:
                raise RuntimeConcurrencyError(f"run {context.run_id} 的 Worker 租约已丢失")

    async def _run_with_lease(
        self,
        context: RunContext,
        execution: Coroutine[Any, Any, str],
    ) -> str:
        if self._lease_store is None:
            return await execution
        execution_task = asyncio.create_task(execution)
        heartbeat_task = asyncio.create_task(self._heartbeat(context))
        try:
            done, _ = await asyncio.wait(
                {execution_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
                context.status = "interrupted"
                context.emit(
                    "run_interrupted",
                    reason="worker_lease_lost",
                    error=str(error) if error is not None else None,
                )
                await self._persist(context)
                if error is not None:
                    raise error
                raise RuntimeConcurrencyError(f"run {context.run_id} 的 Worker 心跳意外结束")
            return await execution_task
        finally:
            if not execution_task.done():
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await self._release(context)

    def _start_stream_heartbeat(
        self,
        context: RunContext,
        errors: list[BaseException],
    ) -> asyncio.Task[None] | None:
        if self._lease_store is None:
            return None
        owner = asyncio.current_task()

        async def monitor() -> None:
            try:
                await self._heartbeat(context)
            except Exception as exc:
                errors.append(exc)
                context.emit("run_lease_lost", worker_id=self._worker_id)
                if owner is not None:
                    owner.cancel()
                raise

        return asyncio.create_task(monitor())

    def _check_access(
        self,
        context: RunContext,
        access: AccessContext | None,
    ) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 AgentRuntime 要求提供 AccessContext")
        if self._require_access and (
            context.user_id is None or context.tenant_id is None
        ):
            raise PermissionError("该 run 尚未绑定用户和租户")
        if access is not None and not access.can_access(context.user_id, context.tenant_id):
            raise PermissionError("无权访问该 run")

    def _require_owner(
        self,
        user_id: str | None,
        tenant_id: str | None,
    ) -> None:
        if self._require_access and (user_id is None or tenant_id is None):
            raise PermissionError("严格访问模式要求同时提供 user_id 和 tenant_id")

    @staticmethod
    def _context_access(context: RunContext) -> AccessContext | None:
        if context.user_id is None or context.tenant_id is None:
            return None
        return AccessContext(user_id=context.user_id, tenant_id=context.tenant_id)

    async def _execute(
        self,
        context: RunContext,
        user_input: str,
        *,
        memory_context: str | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> str:
        context.status = "running"
        context.emit("run_started", input=user_input)
        context.checkpoint_callback = self._checkpoint_callback(context)
        return await self._agent.run(
            user_input,
            memory_context=memory_context,
            thread_id=context.thread_id,
            run_context=context,
            checkpoint_state=checkpoint_state,
        )

    def _checkpoint_callback(self, context: RunContext):
        async def persist(_state: dict[str, Any]) -> None:
            await self._persist(context)

        return persist

    async def _persist(self, context: RunContext) -> None:
        await self._run_store.save_run(context)
        if self._event_sink is None:
            return
        cursor = self._event_cursors.get(context.run_id, 0)
        for event in context.events[cursor:]:
            try:
                await self._event_sink.publish(event)
            except Exception:
                # 事件订阅者故障不能覆盖 Agent 的最终结果。
                logger.exception("发布 Agent 运行事件失败：%s", event.event_type)
        self._event_cursors[context.run_id] = len(context.events)
