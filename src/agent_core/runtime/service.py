"""通用 Agent Runtime 生命周期服务。

Runtime 负责运行实例和生命周期，业务编排仍由调用方注入到工具或上层服务中。
这样 API 层不需要重复实现 start、resume、cancel 和事件发布逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agent_core.ports import EventSink, RunStore
from agent_core.protocol.runtime import RunContext, RunEvent
from agent_core.runtime.react import ReActAgent
from agent_core.runtime.store import MemoryRunStore

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
    ) -> None:
        self._agent = agent
        self._run_store = run_store or MemoryRunStore()
        self._event_sink = event_sink
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
            task = asyncio.create_task(
                self._execute(
                    context,
                    user_input,
                    memory_context=memory_context,
                    checkpoint_state=checkpoint_state,
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
        await self.wait(handle.context.thread_id, handle.context.run_id)
        return await self.get(handle.context.thread_id, handle.context.run_id)

    async def wait(self, thread_id: str, run_id: str) -> RunContext:
        """等待进程内任务；进程重启后可读取持久化状态。"""
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)
        context = await self._run_store.load_run(thread_id, run_id)
        if context is None:
            raise KeyError(f"run 不存在：{run_id}")
        return context

    async def get(self, thread_id: str, run_id: str) -> RunContext:
        """读取运行上下文。"""
        context = await self._run_store.load_run(thread_id, run_id)
        if context is None:
            raise KeyError(f"run 不存在：{run_id}")
        return context

    async def resume(self, thread_id: str, run_id: str) -> RuntimeRun:
        """从已有 checkpoint 继续运行同一个 run。"""
        async with self._lock:
            if run_id in self._tasks:
                return RuntimeRun(
                    context=await self.get(thread_id, run_id),
                    task=self._tasks[run_id],
                )
            context = await self.get(thread_id, run_id)
            if not context.checkpoint:
                raise ValueError("该 run 没有可恢复的 checkpoint")
            context.status = "queued"
            context.emit("run_resumed")
            await self._run_store.save_run(context)
            task = asyncio.create_task(
                self._execute(
                    context,
                    "",
                    checkpoint_state=context.checkpoint,
                )
            )
            self._tasks[run_id] = task
            task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
            return RuntimeRun(context=context, task=task)

    async def cancel(self, thread_id: str, run_id: str) -> bool:
        """取消进程内运行；已完成或只存在于数据库中的 run 不会被伪造修改。"""
        context = await self.get(thread_id, run_id)
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        context = await self.get(thread_id, run_id)
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
        await self._run_store.save_run(context)
        context.status = "running"
        context.emit("run_started", input=user_input)
        context.checkpoint_callback = self._checkpoint_callback(context)
        try:
            async for chunk in self._agent.stream(
                user_input,
                memory_context=kwargs.pop("memory_context", None),
                thread_id=thread_id,
                run_context=context,
                checkpoint_state=kwargs.pop("checkpoint_state", None),
            ):
                yield chunk
        finally:
            await self._persist(context)

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
