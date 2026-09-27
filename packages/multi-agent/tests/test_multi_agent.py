"""多 Agent 编排扩展契约测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_core import (
    AccessContext,
    MemoryEventSink,
    MemoryRunLeaseStore,
    Message,
    RunContext,
    SqliteWorkflowExecutionStore,
    assistant_message,
)

from agent_core_multi_agent import (
    AgentAccessDeniedError,
    AgentDescriptor,
    AgentRegistry,
    AgentResult,
    AgentTask,
    CallableAgentInvoker,
    CapabilityRouter,
    CoordinationBusyError,
    CoordinationExecution,
    CoordinationPolicy,
    HandoffRequest,
    ModelAgentRouter,
    RuntimeAgentInvoker,
    SupervisorAgent,
    WorkflowCoordinationStore,
)


class _RecordingInvoker:
    def __init__(self, agent_id: str, results: list[AgentResult | str]) -> None:
        self.agent_id = agent_id
        self.results = list(results)
        self.tasks: list[AgentTask] = []

    async def invoke(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        value = self.results.pop(0)
        if isinstance(value, AgentResult):
            return AgentResult(
                task_id=task.task_id,
                agent_id=self.agent_id,
                status=value.status,
                output=value.output,
                run_id=value.run_id,
                handoff=value.handoff,
                error=value.error,
                retryable=value.retryable,
                usage=value.usage,
                metadata=value.metadata,
            )
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            status="completed",
            output=value,
            run_id=f"run-{self.agent_id}",
        )


class _RouterModel:
    async def chat(self, messages: list[Message], **_: Any) -> Message:
        self.messages = messages
        return assistant_message('{"agent_id":"writer","reason":"需要整理答案"}')


class RegistryAndRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_callable_invoker_accepts_sync_function(self) -> None:
        invoker = CallableAgentInvoker("sync", lambda task: f"done:{task.input}")
        task = AgentTask(input="work", thread_id="sync-thread")

        result = await invoker.invoke(task)

        self.assertEqual(result.output, "done:work")

    async def test_registry_filters_roles_and_resolves_alias(self) -> None:
        registry = AgentRegistry()
        invoker = _RecordingInvoker("admin-agent", ["ok"])
        descriptor = AgentDescriptor(
            agent_id="admin-agent",
            aliases=frozenset({"admin"}),
            description="管理任务",
            capabilities=frozenset({"audit"}),
            required_roles=frozenset({"tenant_admin"}),
        )
        registry.register(descriptor, invoker)

        with self.assertRaises(AgentAccessDeniedError):
            registry.get_descriptor("admin", access=AccessContext())

        admin = AccessContext(roles=frozenset({"tenant_admin"}))
        self.assertEqual(registry.get_descriptor("admin", access=admin).agent_id, "admin-agent")
        self.assertEqual(registry.candidates("audit", access=admin), (descriptor,))

    async def test_model_router_can_only_select_accessible_candidate(self) -> None:
        registry = AgentRegistry()
        for agent_id in ("researcher", "writer"):
            registry.register(
                AgentDescriptor(agent_id=agent_id, description=agent_id),
                _RecordingInvoker(agent_id, ["ok"]),
            )
        router = ModelAgentRouter(_RouterModel())  # type: ignore[arg-type]

        decision = await router.route(AgentTask(input="整理报告", thread_id="t-1"), registry)

        self.assertEqual(decision.agent_id, "writer")
        self.assertIn("整理答案", decision.reason)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_handoff_chain_persists_parent_lineage_and_events(self) -> None:
        registry = AgentRegistry()
        research = _RecordingInvoker(
            "researcher",
            [
                AgentResult(
                    task_id="placeholder",
                    agent_id="researcher",
                    status="completed",
                    output="资料",
                    run_id="research-run",
                    handoff=HandoffRequest(
                        required_capability="write",
                        input="根据资料撰写结果",
                        reason="检索完成",
                    ),
                    usage={"total_tokens": 10},
                )
            ],
        )
        writer = _RecordingInvoker("writer", ["最终报告"])
        registry.register(
            AgentDescriptor(
                agent_id="researcher",
                description="检索",
                capabilities=frozenset({"research"}),
            ),
            research,
        )
        registry.register(
            AgentDescriptor(
                agent_id="writer",
                description="写作",
                capabilities=frozenset({"write"}),
            ),
            writer,
        )
        sink = MemoryEventSink()
        store = WorkflowCoordinationStore()
        supervisor = SupervisorAgent(registry, store=store, event_sink=sink)
        root = AgentTask(input="研究并撰写", thread_id="thread-1", required_capability="research")

        execution = await supervisor.run(root)

        self.assertEqual(execution.status, "completed")
        self.assertEqual(execution.output, "最终报告")
        self.assertEqual(execution.route_history, ["researcher", "writer"])
        self.assertEqual(execution.handoffs_completed, 1)
        self.assertEqual(writer.tasks[0].parent_task_id, root.task_id)
        self.assertEqual(writer.tasks[0].parent_run_id, "research-run")
        self.assertEqual(writer.tasks[0].lineage, ("researcher",))
        self.assertEqual(execution.usage["total_tokens"], 10)
        self.assertIn("agent_handoff", [event.event_type for event in sink.events])
        loaded = await store.load(execution.execution_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.output, "最终报告")

    async def test_interrupted_execution_resumes_same_routed_agent(self) -> None:
        registry = AgentRegistry()
        invoker = _RecordingInvoker(
            "worker",
            [
                AgentResult(
                    task_id="placeholder",
                    agent_id="worker",
                    status="interrupted",
                    error="等待外部输入",
                ),
                "恢复完成",
            ],
        )
        registry.register(AgentDescriptor(agent_id="worker", description="执行"), invoker)
        supervisor = SupervisorAgent(registry, router=CapabilityRouter("worker"))
        task = AgentTask(input="执行任务", thread_id="thread-resume")

        interrupted = await supervisor.run(task)
        resumed = await supervisor.resume(interrupted.execution_id)

        self.assertEqual(interrupted.status, "interrupted")
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.output, "恢复完成")
        self.assertEqual(resumed.route_history, ["worker"])
        self.assertEqual(invoker.tasks[0].idempotency_key, invoker.tasks[1].idempotency_key)

    async def test_resume_consumes_checkpointed_result_without_reinvoking_agent(self) -> None:
        registry = AgentRegistry()
        research = _RecordingInvoker("researcher", [])
        writer = _RecordingInvoker("writer", ["checkpoint recovered"])
        registry.register(
            AgentDescriptor(agent_id="researcher", description="检索"),
            research,
        )
        registry.register(
            AgentDescriptor(
                agent_id="writer",
                description="写作",
                capabilities=frozenset({"write"}),
            ),
            writer,
        )
        store = WorkflowCoordinationStore()
        task = AgentTask(input="恢复", thread_id="checkpoint")
        bound = task.bind_execution(task.task_id)
        execution = CoordinationExecution(
            execution_id=task.task_id,
            root_task=bound,
            current_task=bound,
            status="interrupted",
            current_agent_id="researcher",
            route_history=["researcher"],
            results=[
                AgentResult(
                    task_id=task.task_id,
                    agent_id="researcher",
                    status="completed",
                    output="资料",
                    run_id="research-run",
                    handoff=HandoffRequest(required_capability="write"),
                )
            ],
        )
        await store.save(execution)
        supervisor = SupervisorAgent(registry, store=store)

        resumed = await supervisor.resume(task.task_id)

        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.output, "checkpoint recovered")
        self.assertEqual(research.tasks, [])
        self.assertEqual(len(writer.tasks), 1)

    async def test_retry_and_usage_budget_are_enforced(self) -> None:
        registry = AgentRegistry()
        invoker = _RecordingInvoker(
            "worker",
            [
                AgentResult(
                    task_id="placeholder",
                    agent_id="worker",
                    status="failed",
                    error="temporary",
                    retryable=True,
                ),
                AgentResult(
                    task_id="placeholder",
                    agent_id="worker",
                    status="completed",
                    output="ok",
                    usage={"total_tokens": 11},
                ),
            ],
        )
        registry.register(AgentDescriptor(agent_id="worker", description="执行"), invoker)
        supervisor = SupervisorAgent(
            registry,
            router=CapabilityRouter("worker"),
            policy=CoordinationPolicy(
                max_agent_attempts=2,
                retry_base_seconds=0,
                max_total_tokens=10,
            ),
        )

        execution = await supervisor.run(AgentTask(input="执行", thread_id="budget"))

        self.assertEqual(execution.status, "failed")
        self.assertIn("Token", execution.error or "")
        self.assertEqual(execution.agent_calls, 2)

    async def test_parallel_execution_respects_global_limit(self) -> None:
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def slow(task: AgentTask) -> str:
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.02)
            async with lock:
                active -= 1
            return task.input

        registry = AgentRegistry()
        registry.register(
            AgentDescriptor(agent_id="worker", description="执行"),
            CallableAgentInvoker("worker", slow),
        )
        supervisor = SupervisorAgent(
            registry,
            router=CapabilityRouter("worker"),
            policy=CoordinationPolicy(max_parallelism=2),
        )

        executions = await supervisor.run_parallel(
            [AgentTask(input=f"task-{index}", thread_id=f"t-{index}") for index in range(5)]
        )

        self.assertEqual([item.output for item in executions], [f"task-{index}" for index in range(5)])
        self.assertEqual(peak, 2)

    async def test_cancel_cannot_be_overwritten_by_late_agent_result(self) -> None:
        started = asyncio.Event()
        never = asyncio.Event()

        async def slow(_task: AgentTask) -> str:
            started.set()
            await never.wait()
            return "late result"

        registry = AgentRegistry()
        registry.register(
            AgentDescriptor(agent_id="worker", description="执行"),
            CallableAgentInvoker("worker", slow),
        )
        supervisor = SupervisorAgent(registry, router=CapabilityRouter("worker"))
        task = AgentTask(input="cancel", thread_id="cancel-thread")
        running = asyncio.create_task(supervisor.run(task))
        await started.wait()

        cancelled = await supervisor.cancel(task.task_id)

        self.assertEqual(cancelled.status, "cancelled")
        with self.assertRaises(asyncio.CancelledError):
            await running
        loaded = await supervisor.get(task.task_id)
        self.assertEqual(loaded.status, "cancelled")
        self.assertNotEqual(loaded.output, "late result")

    async def test_run_lease_blocks_second_worker_without_mutating_state(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(_task: AgentTask) -> str:
            started.set()
            await release.wait()
            return "done"

        registry = AgentRegistry()
        registry.register(
            AgentDescriptor(agent_id="worker", description="执行"),
            CallableAgentInvoker("worker", slow),
        )
        store = WorkflowCoordinationStore()
        leases = MemoryRunLeaseStore()
        first_supervisor = SupervisorAgent(
            registry,
            router=CapabilityRouter("worker"),
            store=store,
            lease_store=leases,
            worker_id="worker-1",
        )
        second_supervisor = SupervisorAgent(
            registry,
            router=CapabilityRouter("worker"),
            store=store,
            lease_store=leases,
            worker_id="worker-2",
        )
        task = AgentTask(input="lease", thread_id="lease-thread")
        running = asyncio.create_task(first_supervisor.run(task))
        await started.wait()

        with self.assertRaises(CoordinationBusyError):
            await second_supervisor.resume(task.task_id)
        snapshot = await store.load(task.task_id)
        assert snapshot is not None
        self.assertEqual(snapshot.status, "running")

        release.set()
        completed = await running
        self.assertEqual(completed.status, "completed")


class AdapterAndStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_adapter_propagates_scope_and_parent_run(self) -> None:
        captured: dict[str, Any] = {}

        class _Runtime:
            async def run(self, thread_id: str, user_input: str, **kwargs: Any) -> RunContext:
                captured.update({"thread_id": thread_id, "input": user_input, **kwargs})
                context = RunContext(thread_id=thread_id, run_id="child-run", status="completed")
                context.emit("run_completed", answer="runtime answer")
                return context

        invoker = RuntimeAgentInvoker("runtime-agent", _Runtime())  # type: ignore[arg-type]
        access = AccessContext(user_id="alice", tenant_id="tenant-a")
        task = AgentTask(
            input="hello",
            thread_id="thread-runtime",
            access=access,
            coordination_id="coord-1",
        )

        result = await invoker.invoke(task)

        self.assertEqual(result.output, "runtime answer")
        self.assertEqual(captured["parent_run_id"], "coord-1")
        self.assertEqual(captured["user_id"], "alice")
        self.assertEqual(captured["tenant_id"], "tenant-a")

    async def test_runtime_adapter_resumes_existing_interrupted_run(self) -> None:
        interrupted = RunContext(
            thread_id="thread-runtime-resume",
            run_id="child-run",
            status="interrupted",
            checkpoint={"messages": []},
        )
        completed = RunContext(
            thread_id="thread-runtime-resume",
            run_id="child-run",
            status="completed",
            checkpoint={"messages": []},
        )
        completed.emit("run_completed", answer="resumed answer")

        class _Runtime:
            def __init__(self) -> None:
                self.resume_calls = 0

            async def run(self, _thread_id: str, _user_input: str, **_kwargs: Any) -> RunContext:
                return interrupted

            async def resume(self, _thread_id: str, _run_id: str, **_kwargs: Any) -> object:
                self.resume_calls += 1
                return object()

            async def wait(self, _thread_id: str, _run_id: str, **_kwargs: Any) -> RunContext:
                return completed

        runtime = _Runtime()
        invoker = RuntimeAgentInvoker("runtime-agent", runtime)  # type: ignore[arg-type]
        task = AgentTask(
            input="resume",
            thread_id="thread-runtime-resume",
            coordination_id="coord-resume",
            resume_requested=True,
        )

        result = await invoker.invoke(task)

        self.assertEqual(runtime.resume_calls, 1)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output, "resumed answer")

    async def test_sqlite_store_persists_and_isolates_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "multi-agent.db"
            access = AccessContext(user_id="alice", tenant_id="tenant-a")
            other = AccessContext(user_id="bob", tenant_id="tenant-a")
            first = WorkflowCoordinationStore(
                SqliteWorkflowExecutionStore(path),
                require_access=True,
            )
            task = AgentTask(input="persist", thread_id="thread", access=access)
            value = CoordinationExecution(
                root_task=task.bind_execution(task.task_id),
                current_task=task.bind_execution(task.task_id),
                execution_id=task.task_id,
            )
            await first.save(value)
            second = WorkflowCoordinationStore(
                SqliteWorkflowExecutionStore(path),
                require_access=True,
            )

            loaded = await second.load(task.task_id, access=access)

            self.assertIsNotNone(loaded)
            with self.assertRaises(PermissionError):
                await second.load(task.task_id, access=other)

    async def test_memory_store_rejects_non_json_context_like_sqlite(self) -> None:
        store = WorkflowCoordinationStore()
        task = AgentTask(input="invalid", thread_id="thread", context={"value": object()})
        value = CoordinationExecution(
            root_task=task.bind_execution(task.task_id),
            current_task=task.bind_execution(task.task_id),
            execution_id=task.task_id,
        )

        with self.assertRaisesRegex(TypeError, "JSON"):
            await store.save(value)


if __name__ == "__main__":
    unittest.main()
