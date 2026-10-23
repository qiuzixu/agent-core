"""通用 Agent Runtime 生命周期服务。

Runtime 负责运行实例和生命周期，业务编排仍由调用方注入到工具或上层服务中。
这样 API 层不需要重复实现 start、resume、cancel 和事件发布逻辑。
"""

from __future__ import annotations
import inspect

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent_core.access import AccessContext, enforce_access
from agent_core.callbacks import CallbackHandler, CallbackManager
from agent_core.ports import EventSink, RecoverableRunStore, RunStore
from agent_core.protocol.runtime import RunContext, RunEvent, RunStatus
from agent_core.runtime.state import transition_run_status
from agent_core.runtime.react import ReActAgent
from agent_core.runtime.store import MemoryRunStore
from agent_core.storage.lease import RunLeaseStore
from agent_core.storage.runtime import RuntimeConcurrencyError

logger = logging.getLogger(__name__)


class RunExecutor(Protocol):
    """自定义 Run 执行器：在 Run 生命周期内替代默认的 ``agent.run`` 调用。

    Runtime 会在挂接执行器前完成状态置位、``run_started`` 事件、checkpoint
    回调绑定和持久化；执行器只需驱动具体的 Agent 实现并返回最终回答。
    应用可用它接入按模型快照选择 Agent、业务编排分支等自定义执行策略。
    """

    async def __call__(
        self,
        context: RunContext,
        user_input: str,
        *,
        memory_context: str | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> str: ...


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
        callbacks: Sequence[CallbackHandler] | None = None,
        callback_manager: CallbackManager | None = None,
        lease_store: RunLeaseStore | None = None,
        worker_id: str | None = None,
        lease_seconds: float = 30.0,
        heartbeat_seconds: float = 10.0,
        require_access: bool = False,
        run_executor: RunExecutor | None = None,
    ) -> None:
        # 启动前校验生命周期参数：心跳过慢会导致租约在业务完成前被其他 Worker 抢走。
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds 必须大于 0 且小于 lease_seconds")
        # callbacks 是便捷注入；callback_manager 是显式接管。两者只允许选择一种。
        if callbacks is not None and callback_manager is not None:
            raise ValueError("callbacks 和 callback_manager 不能同时传入")
        self._agent = agent
        # 未注入持久化时使用内存实现，适合测试和最小开发环境。
        store = run_store or MemoryRunStore()
        self._run_store = store
        # 后台重试调度器依赖可选扩展协议；这里在构造时显式判定能力。
        self._recoverable_run_store = store if isinstance(store, RecoverableRunStore) else None
        self._event_sink = event_sink
        self._callback_manager = callback_manager or CallbackManager(callbacks)
        self._lease_store = lease_store
        self._worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._require_access = require_access
        # 自定义执行器可以替换默认 agent.run，但状态/事件/持久化仍由 Runtime 管理。
        self._run_executor = run_executor
        # _tasks 是进程内句柄；进程重启后不能恢复，需要通过 checkpoint + resume 恢复。
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
        parent_run_id: str | None = None,
        tags: Sequence[str] | None = None,
        idempotency_key: str | None = None,
        memory_context: str | None = None,
        metadata: dict[str, Any] | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> RuntimeRun:
        """创建后台运行实例并立即返回句柄。"""
        # 严格访问模式要求调用方声明身份，便于后续做 Owner 校验和审计。
        self._require_owner(user_id, tenant_id)
        # 同一进程内用锁串行化 start/resume，避免同一 run 被并发创建。
        async with self._lock:
            if idempotency_key:
                existing = await self._run_store.find_run_by_idempotency(tenant_id, user_id, idempotency_key)
                if existing is not None:
                    task = self._tasks.get(existing.run_id)
                    return RuntimeRun(context=existing, task=task)

            # 先构造 RunContext 并持久化，保证租约/后台任务失败时也能看到初始记录。
            context = RunContext(
                thread_id=thread_id,
                run_id=str(uuid.uuid4()),
                user_id=user_id,
                tenant_id=tenant_id,
                parent_run_id=parent_run_id,
                tags=list(tags or ()),
                idempotency_key=idempotency_key,
                metadata={
                    **(metadata or {}),
                    "_input": user_input,
                },
                checkpoint=checkpoint_state or {},
            )
            await self._run_store.save_run(context)
            # 多实例部署时先抢 Worker 租约；抢不到说明同一 run 已由其他实例负责。
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
            # done callback 只清理进程内句柄；持久化状态由任务执行链负责收口。
            self._tasks[context.run_id] = task
            task.add_done_callback(lambda _: self._cleanup_run(context.run_id))
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
        # shield 的作用：外部调用方超时/取消时，不立刻取消正在执行的 Agent 任务。
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
            # 进程内任务还在时直接返回原句柄，避免同一 checkpoint 被双写。
            if run_id in self._tasks:
                return RuntimeRun(
                    context=await self.get(thread_id, run_id, access=access),
                    task=self._tasks[run_id],
                )
            context = await self.get(thread_id, run_id, access=access)
            # 只有存在快照且不是终态的 run 才允许 resume。
            if not context.checkpoint:
                raise ValueError("该 run 没有可恢复的 checkpoint")
            if context.status in {"completed", "cancelled"}:
                raise ValueError(f"状态为 {context.status} 的 run 不能恢复")
            # 事件游标从已有事件长度开始，避免 resume 后重复发布历史事件。
            self._event_cursors.setdefault(context.run_id, len(context.events))
            await self._claim(context)
            try:
                transition_run_status(context, "queued")
                context.emit("run_resumed")
                await self._persist(context)
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
            task.add_done_callback(lambda _: self._cleanup_run(run_id))
            return RuntimeRun(context=context, task=task)

    @staticmethod
    def _is_retry_due(context: RunContext) -> bool:
        # 业务工具失败时可声明 retry_after；到点后由调度器统一唤醒。
        pending_retry = context.state.get("pending_retry") or {}
        next_retry_at = str(pending_retry.get("next_retry_at") or "")
        if not next_retry_at:
            return False
        try:
            due = datetime.fromisoformat(next_retry_at)
        except ValueError:
            return False
        # 兼容旧数据里的无时区时间：默认按 UTC 处理，避免本机时区漂移。
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        return due <= datetime.now(UTC)

    async def resume_due_retries(self, *, limit: int = 100) -> list[RuntimeRun]:
        """唤醒到点的 waiting_retry 任务；多实例部署时应由调度器调用。"""
        # 只扫描 waiting_retry，不主动重跑 completed/failed/cancelled。
        if limit <= 0:
            return []
        # list_runs_by_status 是可选扩展；简易 MemoryRunStore/老后端可以不实现。
        list_by_status = getattr(self._run_store, "list_runs_by_status", None)
        if not callable(list_by_status):
            return []
        candidates_outcome = list_by_status("waiting_retry")
        if not inspect.isawaitable(candidates_outcome):
            raise TypeError("RunStore.list_runs_by_status 必须返回可等待结果")
        candidates = await candidates_outcome
        resumed: list[RuntimeRun] = []
        for context in candidates:
            if len(resumed) >= limit:
                break
            if not self._is_retry_due(context):
                continue
            try:
                handle = await self.resume(
                    context.thread_id,
                    context.run_id,
                    access=self._context_access(context),
                )
            except Exception:
                logger.exception("恢复到点的重试任务失败：%s/%s", context.thread_id, context.run_id)
                continue
            resumed.append(handle)
        return resumed

    def start_retry_scheduler(
        self,
        *,
        interval_seconds: float = 30.0,
        limit: int = 100,
    ) -> asyncio.Task[None]:
        """启动内置轮询调度器；生产多实例部署也可以用独立 Worker 调用。"""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于 0")

        async def loop() -> None:
            while True:
                try:
                    await self.resume_due_retries(limit=limit)
                except Exception:
                    logger.exception("重试调度器扫描失败")
                await asyncio.sleep(interval_seconds)

        return asyncio.create_task(loop(), name="agent-core-retry-scheduler")

    async def cancel(
        self,
        thread_id: str,
        run_id: str,
        *,
        access: AccessContext | None = None,
    ) -> bool:
        """取消进程内运行；已完成或只存在于数据库中的 run 不会被伪造修改。"""
        context = await self.get(thread_id, run_id, access=access)
        # 只能取消当前进程拥有的任务；跨实例取消应走中断标记/审批平台/队列控制面。
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        # 等任务真正收口完成，再读取最终状态；否则可能读到旧的 running。
        gather_result = await asyncio.gather(task, return_exceptions=True)
        for outcome in gather_result:
            # 取消方不再吞掉业务异常；这里记录日志，避免取消失败与取消成功不可区分。
            if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                logger.warning("被取消的 run %s 在结束前抛出异常", run_id, exc_info=outcome)
        context = await self.get(thread_id, run_id, access=access)
        return context.status == "cancelled"

    async def stream(
        self,
        thread_id: str,
        user_input: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """以流式方式执行一次运行，并复用同一生命周期持久化逻辑。"""
        # stream 不创建后台任务：上游消费者就是这个请求协程，生命周期随 SSE/WebSocket 连接。
        context = RunContext(
            thread_id=thread_id,
            run_id=str(uuid.uuid4()),
            user_id=kwargs.pop("user_id", None),
            tenant_id=kwargs.pop("tenant_id", None),
            parent_run_id=kwargs.pop("parent_run_id", None),
            tags=list(kwargs.pop("tags", ())),
            idempotency_key=kwargs.pop("idempotency_key", None),
            metadata={**kwargs.pop("metadata", {}), "_input": user_input},
        )
        self._require_owner(context.user_id, context.tenant_id)
        await self._run_store.save_run(context)
        await self._claim(context)
        transition_run_status(context, "running")
        context.emit("run_started", input=user_input)
        context.checkpoint_callback = self._checkpoint_callback(context)
        await self._persist(context)
        # 流式也要续租；否则长回答/长工具执行期间租约可能过期。
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
                # 事件先进入 context.events，再立即发布；chunk 最后交给调用方转 SSE。
                context.emit("model_stream_chunk", component="model", delta=chunk)
                await self._publish_pending_events(context)
                yield chunk
        except asyncio.CancelledError:
            # 普通取消透传；租约丢失导致的取消要转成明确的 interrupted 语义。
            if not lease_errors:
                raise
            transition_run_status(context, "interrupted")
            context.emit(
                "run_interrupted",
                reason="worker_lease_lost",
                error=str(lease_errors[0]),
            )
            raise RuntimeConcurrencyError(f"run {context.run_id} 的 Worker 租约已丢失") from lease_errors[0]
        finally:
            # 无论成功、失败、取消，都要停止心跳并释放租约；否则会阻塞其他实例恢复。
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await self._release(context)
            await self._persist(context)
            self._event_cursors.pop(context.run_id, None)

    async def recover_expired_runs(self, *, auto_resume: bool = False) -> list[RunContext]:
        """标记租约过期的 Run，并可从 checkpoint 自动恢复。"""
        # 没配租约时假设单写者部署，不做过期恢复。
        if self._lease_store is None:
            return []
        recovered: list[RunContext] = []
        # 先读业务状态，再释放过期租约；随后只有 queued/running 才标记中断。
        for lease in await self._lease_store.list_expired():
            context = await self._run_store.load_run(lease.thread_id, lease.run_id)
            await self._lease_store.release(lease.thread_id, lease.run_id, lease.worker_id)
            if context is None or context.status not in {"queued", "running"}:
                continue
            self._event_cursors.setdefault(context.run_id, len(context.events))
            transition_run_status(context, "interrupted")
            context.emit("run_interrupted", reason="worker_lease_expired")
            await self._persist(context)
            recovered.append(context)
            if auto_resume and context.checkpoint:
                await self.resume(
                    context.thread_id,
                    context.run_id,
                    access=self._context_access(context),
                )
        return recovered

    async def _claim(self, context: RunContext) -> None:
        # 多实例互斥的核心：同一 run 同一时刻只允许一个 Worker 执行。
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
        # 周期续租。一旦续租失败，立即终止当前执行，防止双写。
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
        # 正式路径：业务执行与租约心跳并发，谁先失败都尽快收口。
        execution_task = asyncio.create_task(execution)
        heartbeat_task = asyncio.create_task(self._heartbeat(context))
        try:
            done, _ = await asyncio.wait(
                {execution_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 两个任务可能在同一轮事件循环同时完成；执行结果优先，避免丢失成功终态。
            if execution_task in done:
                return await execution_task
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                context.metadata["_lease_lost"] = True
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
                # 取消/完成可能和租约失败几乎同时发生；非终态必须落成 interrupted。
                if context.status not in {"completed", "failed", "cancelled"}:
                    transition_run_status(context, "interrupted")
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
        # stream 不能包一层 task，因此用独立监控任务感知租约失败并取消当前流。
        if self._lease_store is None:
            return None
        owner = asyncio.current_task()

        async def monitor() -> None:
            try:
                await self._heartbeat(context)
            except Exception as exc:
                errors.append(exc)
                # 让 Agent 层识别这次取消来自租约丢失，避免它抢先保存 cancelled。
                context.metadata["_lease_lost"] = True
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
        enforce_access(
            strict=self._require_access,
            accessor="AgentRuntime",
            subject="run",
            owner_user_id=context.user_id,
            owner_tenant_id=context.tenant_id,
            access=access,
        )

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
        transition_run_status(context, "running")
        context.emit("run_started", input=user_input)
        context.checkpoint_callback = self._checkpoint_callback(context)
        await self._persist(context)
        try:
            if self._run_executor is not None:
                # 自定义执行器：生命周期（状态/事件/持久化/租约）仍由 Runtime 收口。
                answer = await self._run_executor(
                    context,
                    user_input,
                    memory_context=memory_context,
                    checkpoint_state=checkpoint_state,
                )
            else:
                answer = await self._agent.run(
                    user_input,
                    memory_context=memory_context,
                    thread_id=context.thread_id,
                    run_context=context,
                    checkpoint_state=checkpoint_state,
                )
        except asyncio.CancelledError:
            # Worker 租约丢失导致的取消要交给 _run_with_lease 标记 interrupted，
            # 不能在这里抢先落成 cancelled 终态。
            if context.status == "running" and not context.metadata.get("_lease_lost", False):
                await self._finish_terminal(context, "cancelled", "run_cancelled")
            raise
        except Exception as exc:
            if context.status == "running":
                await self._finish_terminal(context, "failed", "run_failed", error=str(exc))
            raise
        # 内置路径由 Agent Loop 自己收口；执行器路径在此统一落到 completed。
        if context.status == "running":
            await self._finish_terminal(context, "completed", "run_completed", answer=answer)
        return answer

    async def _finish_terminal(
        self,
        context: RunContext,
        status: RunStatus,
        event_type: str,
        **payload: Any,
    ) -> None:
        transition_run_status(context, status)
        context.emit(event_type, **payload)
        await self._persist(context)

    def _checkpoint_callback(
        self,
        context: RunContext,
    ) -> Callable[[dict[str, Any]], Awaitable[None]]:
        async def persist(_state: dict[str, Any]) -> None:
            await self._persist(context)

        return persist

    async def _persist(self, context: RunContext) -> None:
        # 保存状态和发布事件放在同一个运行时入口；Agent 只负责产出事件，不直接触达存储。
        await self._run_store.save_run(context)
        await self._publish_pending_events(context)

    async def _publish_pending_events(self, context: RunContext) -> None:
        """把尚未发布的事件依次交给进程内 Callback 和进程外 EventSink。"""

        # cursor 记录“已发布到多少号事件”，保证重试/恢复时不重复发送。
        cursor = self._event_cursors.get(context.run_id, 0)
        for event in context.events[cursor:]:
            await self._callback_manager.dispatch(event)
            if self._event_sink is not None:
                try:
                    await self._event_sink.publish(event)
                except Exception:
                    # 事件订阅者故障不能覆盖 Agent 的最终结果。
                    logger.exception("发布 Agent 运行事件失败：%s", event.event_type)
        self._event_cursors[context.run_id] = len(context.events)

    def _cleanup_run(self, run_id: str) -> None:
        """后台任务完全结束后释放进程内句柄和事件游标。"""
        self._tasks.pop(run_id, None)
        self._event_cursors.pop(run_id, None)
