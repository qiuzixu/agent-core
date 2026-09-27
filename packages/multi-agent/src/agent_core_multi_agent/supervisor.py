"""多 Agent Supervisor、handoff 循环和恢复控制。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import replace

from agent_core import (
    AccessContext,
    CallbackHandler,
    CallbackManager,
    EventSink,
    RunEvent,
    RunLeaseStore,
)

from agent_core_multi_agent.errors import (
    CoordinationBudgetExceededError,
    CoordinationBusyError,
    CoordinationLeaseLostError,
)
from agent_core_multi_agent.protocols import AgentRouter, CoordinationStore
from agent_core_multi_agent.registry import AgentRegistry
from agent_core_multi_agent.routing import CapabilityRouter
from agent_core_multi_agent.storage import WorkflowCoordinationStore
from agent_core_multi_agent.types import (
    AgentResult,
    AgentTask,
    CoordinationExecution,
    CoordinationPolicy,
)

logger = logging.getLogger(__name__)


class SupervisorAgent:
    """路由任务、执行子 Agent、处理 handoff，并在边界保存快照。"""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        router: AgentRouter | None = None,
        store: CoordinationStore | None = None,
        policy: CoordinationPolicy | None = None,
        event_sink: EventSink | None = None,
        callbacks: Sequence[CallbackHandler] | None = None,
        callback_manager: CallbackManager | None = None,
        lease_store: RunLeaseStore | None = None,
        worker_id: str | None = None,
    ) -> None:
        if callbacks is not None and callback_manager is not None:
            raise ValueError("callbacks 和 callback_manager 不能同时传入")
        self._registry = registry
        self._router = router or CapabilityRouter()
        self._policy = policy or CoordinationPolicy()
        self._store = store or WorkflowCoordinationStore(require_access=self._policy.require_access)
        self._event_sink = event_sink
        self._callback_manager = callback_manager or CallbackManager(callbacks)
        self._lease_store = lease_store
        self._worker_id = worker_id or f"multi-agent-{uuid.uuid4()}"
        self._create_lock = asyncio.Lock()
        self._active_invocations: dict[str, asyncio.Task[AgentResult]] = {}

    async def run(self, task: AgentTask) -> CoordinationExecution:
        """创建并同步执行一个协调实例；相同 task_id 会返回已有实例。"""
        self._require_access(task.access)
        async with self._create_lock:
            existing = await self._store.load(task.task_id, access=task.access)
            if existing is not None:
                return existing
            bound_task = task.bind_execution(task.task_id)
            execution = CoordinationExecution(
                execution_id=task.task_id,
                root_task=bound_task,
                current_task=bound_task,
            )
            event = execution.emit("coordination_queued", task_id=bound_task.task_id)
            await self._save_and_publish(execution, event)
        return await self._execute_with_lease(execution)

    async def resume(
        self,
        execution_id: str,
        *,
        access: AccessContext | None = None,
    ) -> CoordinationExecution:
        """从最后保存的 Agent 边界继续执行。"""
        execution = await self.get(execution_id, access=access)
        if execution.status in {"completed", "cancelled"}:
            return execution
        if execution.current_task is None:
            return await self._fail(execution, "协调实例没有可恢复的 current_task")
        return await self._execute_with_lease(execution, resume=True)

    async def get(
        self,
        execution_id: str,
        *,
        access: AccessContext | None = None,
    ) -> CoordinationExecution:
        execution = await self._store.load(execution_id, access=access)
        if execution is None:
            raise KeyError(f"协调实例不存在：{execution_id}")
        self._check_execution_access(execution, access)
        return execution

    async def cancel(
        self,
        execution_id: str,
        *,
        access: AccessContext | None = None,
    ) -> CoordinationExecution:
        """取消协调实例，并取消当前进程内正在等待的子 Agent。"""
        execution = await self.get(execution_id, access=access)
        if execution.status in {"completed", "failed", "cancelled"}:
            return execution
        execution.status = "cancelled"
        event = execution.emit("coordination_cancelled", agent_id=execution.current_agent_id)
        await self._save_and_publish(execution, event)
        active = self._active_invocations.get(execution_id)
        if active is not None:
            active.cancel()
        return execution

    async def run_sequence(self, tasks: Sequence[AgentTask]) -> list[CoordinationExecution]:
        """按输入顺序执行多个独立协调任务。"""
        return [await self.run(task) for task in tasks]

    async def run_parallel(self, tasks: Sequence[AgentTask]) -> list[CoordinationExecution]:
        """在全局并行预算内执行多个独立协调任务，并保持结果顺序。"""
        semaphore = asyncio.Semaphore(self._policy.max_parallelism)

        async def execute(task: AgentTask) -> CoordinationExecution:
            async with semaphore:
                return await self.run(task)

        return list(await asyncio.gather(*(execute(task) for task in tasks)))

    async def _execute_with_lease(
        self,
        execution: CoordinationExecution,
        *,
        resume: bool = False,
    ) -> CoordinationExecution:
        lease_lost = asyncio.Event()
        heartbeat_stop = asyncio.Event()
        heartbeat: asyncio.Task[None] | None = None
        if self._lease_store is not None:
            claimed = await self._lease_store.claim(
                execution.root_task.thread_id,
                execution.execution_id,
                self._worker_id,
                ttl_seconds=self._policy.lease_seconds,
            )
            if not claimed:
                raise CoordinationBusyError(f"协调实例正由其他 Worker 执行：{execution.execution_id}")
            heartbeat = asyncio.create_task(
                self._heartbeat(execution, heartbeat_stop, lease_lost)
            )
        try:
            if resume:
                execution.status = "queued"
                execution.error = None
                assert execution.current_task is not None
                execution.current_task = replace(
                    execution.current_task,
                    resume_requested=True,
                )
                event = execution.emit(
                    "coordination_resumed",
                    task_id=execution.current_task.task_id,
                )
                await self._save_and_publish(execution, event)
            return await self._drive(execution, lease_lost)
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            if self._lease_store is not None:
                try:
                    await self._lease_store.release(
                        execution.root_task.thread_id,
                        execution.execution_id,
                        self._worker_id,
                    )
                except Exception:
                    logger.exception("释放多 Agent 协调租约失败：%s", execution.execution_id)

    async def _heartbeat(
        self,
        execution: CoordinationExecution,
        stop: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        assert self._lease_store is not None
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._policy.heartbeat_seconds)
                return
            except TimeoutError:
                try:
                    renewed = await self._lease_store.renew(
                        execution.root_task.thread_id,
                        execution.execution_id,
                        self._worker_id,
                        ttl_seconds=self._policy.lease_seconds,
                    )
                except Exception:
                    logger.exception("续租多 Agent 协调实例失败：%s", execution.execution_id)
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return

    async def _drive(
        self,
        execution: CoordinationExecution,
        lease_lost: asyncio.Event,
    ) -> CoordinationExecution:
        execution.status = "running"
        event = execution.emit("coordination_started")
        await self._save_and_publish(execution, event)
        try:
            while execution.current_task is not None:
                if lease_lost.is_set():
                    raise CoordinationLeaseLostError("协调实例租约已丢失")
                task = execution.current_task
                agent_id = execution.current_agent_id
                if agent_id is None:
                    decision = await self._router.route(task, self._registry)
                    agent_id = decision.agent_id
                    self._check_visit_budget(execution, agent_id)
                    execution.current_agent_id = agent_id
                    execution.route_history.append(agent_id)
                    event = execution.emit(
                        "agent_routed",
                        task_id=task.task_id,
                        agent_id=agent_id,
                        reason=decision.reason,
                    )
                    await self._save_and_publish(execution, event)

                result = self._checkpointed_result(execution, task, agent_id)
                if result is None:
                    result = await self._invoke_with_retries(
                        execution,
                        task,
                        agent_id,
                        lease_lost,
                    )
                if result.status == "completed" and result.handoff is not None:
                    if execution.handoffs_completed >= self._policy.max_handoffs:
                        raise CoordinationBudgetExceededError("超过最大 handoff 次数")
                    child = task.handoff(
                        result.handoff,
                        current_agent_id=agent_id,
                        parent_run_id=result.run_id,
                        fallback_input=result.output or task.input,
                    )
                    execution.handoffs_completed += 1
                    execution.current_task = child
                    execution.current_agent_id = None
                    event = execution.emit(
                        "agent_handoff",
                        from_agent_id=agent_id,
                        target_agent_id=child.target_agent_id,
                        required_capability=child.required_capability,
                        parent_task_id=task.task_id,
                        child_task_id=child.task_id,
                        reason=result.handoff.reason,
                    )
                    await self._save_and_publish(execution, event)
                    continue

                if result.status == "completed":
                    execution.status = "completed"
                    execution.output = result.output
                    execution.current_task = None
                    execution.current_agent_id = None
                    event = execution.emit(
                        "coordination_completed",
                        agent_id=agent_id,
                        output=result.output,
                    )
                    await self._save_and_publish(execution, event)
                    return execution

                if result.status == "interrupted":
                    execution.status = "interrupted"
                    execution.error = result.error
                    event = execution.emit(
                        "coordination_interrupted",
                        agent_id=agent_id,
                        error=result.error,
                    )
                    await self._save_and_publish(execution, event)
                    return execution

                if result.status == "cancelled":
                    execution.status = "cancelled"
                    event = execution.emit("coordination_cancelled", agent_id=agent_id)
                    await self._save_and_publish(execution, event)
                    return execution

                return await self._fail(execution, result.error or f"Agent {agent_id} 执行失败")
        except CoordinationLeaseLostError as exc:
            execution.status = "interrupted"
            execution.error = str(exc)
            event = execution.emit("coordination_interrupted", error=str(exc), reason="lease_lost")
            await self._save_and_publish(execution, event)
            return execution
        except asyncio.CancelledError:
            latest = await self._store.load(execution.execution_id, access=execution.access)
            if latest is None or latest.status != "cancelled":
                execution.status = "cancelled"
                event = execution.emit("coordination_cancelled", reason="task_cancelled")
                await self._save_and_publish(execution, event)
            raise
        except Exception as exc:
            return await self._fail(execution, str(exc))
        return execution

    async def _invoke_with_retries(
        self,
        execution: CoordinationExecution,
        task: AgentTask,
        agent_id: str,
        lease_lost: asyncio.Event,
    ) -> AgentResult:
        previous_failures = [
            item
            for item in execution.results
            if item.task_id == task.task_id
            and item.agent_id == agent_id
            and item.status == "failed"
        ]
        result = previous_failures[-1] if previous_failures else None
        if result is not None and (
            not result.retryable
            or len(previous_failures) >= self._policy.max_agent_attempts
        ):
            return result
        first_attempt = len(previous_failures) + 1
        for attempt in range(first_attempt, self._policy.max_agent_attempts + 1):
            if execution.agent_calls >= self._policy.max_agent_calls:
                raise CoordinationBudgetExceededError("超过最大 Agent 调用次数")
            execution.agent_calls += 1
            event = execution.emit(
                "agent_started",
                task_id=task.task_id,
                agent_id=agent_id,
                attempt=attempt,
                idempotency_key=task.idempotency_key,
            )
            await self._save_and_publish(execution, event)
            try:
                result = await self._invoke_once(execution, task, agent_id, lease_lost)
            except TimeoutError:
                result = AgentResult(
                    task_id=task.task_id,
                    agent_id=agent_id,
                    status="failed",
                    error=f"Agent 调用超过 {self._policy.agent_timeout_seconds:g} 秒",
                    retryable=True,
                )
            except asyncio.CancelledError:
                raise
            except CoordinationLeaseLostError:
                raise
            except Exception as exc:
                result = AgentResult(
                    task_id=task.task_id,
                    agent_id=agent_id,
                    status="failed",
                    error=str(exc),
                    retryable=True,
                )

            execution.results.append(result)
            self._merge_usage(execution, result)
            event_type = "agent_completed" if result.status == "completed" else f"agent_{result.status}"
            event = execution.emit(
                event_type,
                task_id=task.task_id,
                agent_id=agent_id,
                attempt=attempt,
                run_id=result.run_id,
                error=result.error,
                retryable=result.retryable,
                usage=result.usage,
            )
            await self._save_and_publish(execution, event)
            self._check_usage_budget(execution)

            should_retry = (
                result.status == "failed"
                and result.retryable
                and attempt < self._policy.max_agent_attempts
            )
            if not should_retry:
                return result
            retry_event = execution.emit(
                "agent_retry_scheduled",
                task_id=task.task_id,
                agent_id=agent_id,
                next_attempt=attempt + 1,
            )
            await self._save_and_publish(execution, retry_event)
            delay = self._policy.retry_base_seconds * (2 ** (attempt - 1))
            if delay > 0:
                await asyncio.sleep(delay)
        assert result is not None
        return result

    @staticmethod
    def _checkpointed_result(
        execution: CoordinationExecution,
        task: AgentTask,
        agent_id: str,
    ) -> AgentResult | None:
        """恢复时复用已落库的终态结果，避免重复调用已完成的子 Agent。"""
        if not execution.results:
            return None
        result = execution.results[-1]
        if (
            result.task_id == task.task_id
            and result.agent_id == agent_id
            and result.status in {"completed", "cancelled"}
        ):
            return result
        return None

    async def _invoke_once(
        self,
        execution: CoordinationExecution,
        task: AgentTask,
        agent_id: str,
        lease_lost: asyncio.Event,
    ) -> AgentResult:
        invoke_task = asyncio.create_task(
            asyncio.wait_for(
                self._registry.invoke(agent_id, task),
                timeout=self._policy.agent_timeout_seconds,
            )
        )
        self._active_invocations[execution.execution_id] = invoke_task
        lease_task: asyncio.Task[bool] | None = None
        try:
            if self._lease_store is None:
                return await invoke_task
            lease_task = asyncio.create_task(lease_lost.wait())
            done, _ = await asyncio.wait(
                {invoke_task, lease_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_task in done and lease_lost.is_set():
                invoke_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await invoke_task
                raise CoordinationLeaseLostError("协调实例租约已丢失")
            return await invoke_task
        finally:
            if lease_task is not None:
                lease_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease_task
            self._active_invocations.pop(execution.execution_id, None)

    async def _heartbeat_event(self, event: RunEvent) -> None:
        await self._callback_manager.dispatch(event)
        if self._event_sink is not None:
            try:
                await self._event_sink.publish(event)
            except Exception:
                logger.exception("发布多 Agent 事件失败：%s", event.event_type)

    async def _save_and_publish(
        self,
        execution: CoordinationExecution,
        event: RunEvent,
    ) -> None:
        existing = await self._store.load(execution.execution_id, access=execution.access)
        if (
            existing is not None
            and existing.status == "cancelled"
            and execution.status != "cancelled"
        ):
            raise asyncio.CancelledError
        await self._store.save(execution)
        await self._heartbeat_event(event)

    async def _fail(
        self,
        execution: CoordinationExecution,
        error: str,
    ) -> CoordinationExecution:
        execution.status = "failed"
        execution.error = error
        event = execution.emit(
            "coordination_failed",
            agent_id=execution.current_agent_id,
            error=error,
        )
        await self._save_and_publish(execution, event)
        return execution

    def _require_access(self, access: AccessContext | None) -> None:
        if self._policy.require_access and access is None:
            raise PermissionError("严格访问模式要求提供 AccessContext")

    def _check_execution_access(
        self,
        execution: CoordinationExecution,
        access: AccessContext | None,
    ) -> None:
        owner = execution.access
        self._require_access(access)
        if self._policy.require_access and owner is None:
            raise PermissionError("该协调实例尚未绑定用户和租户")
        if owner is not None and access is not None and not access.can_access(
            owner.user_id,
            owner.tenant_id,
        ):
            raise PermissionError("无权访问该协调实例")

    def _check_visit_budget(self, execution: CoordinationExecution, agent_id: str) -> None:
        if execution.route_history.count(agent_id) >= self._policy.max_visits_per_agent:
            raise CoordinationBudgetExceededError(
                f"Agent {agent_id} 超过最大访问次数 {self._policy.max_visits_per_agent}"
            )

    def _merge_usage(self, execution: CoordinationExecution, result: AgentResult) -> None:
        for key, value in result.usage.items():
            execution.usage[key] = execution.usage.get(key, 0.0) + value

    def _check_usage_budget(self, execution: CoordinationExecution) -> None:
        if self._policy.max_total_tokens is not None:
            total_tokens = execution.usage.get("total_tokens", 0.0)
            if total_tokens > self._policy.max_total_tokens:
                raise CoordinationBudgetExceededError("超过最大 Token 预算")
        if self._policy.max_total_cost is not None:
            total_cost = execution.usage.get("cost", 0.0)
            if total_cost > self._policy.max_total_cost:
                raise CoordinationBudgetExceededError("超过最大费用预算")


__all__ = ["SupervisorAgent"]
