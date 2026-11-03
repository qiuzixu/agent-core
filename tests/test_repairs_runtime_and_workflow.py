"""运行时、工作流和工具执行修复的回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.hitl import ApprovalQueue
from agent_core.hitl.middleware import HumanInTheLoopMiddleware
from agent_core.middleware import MiddlewareAction, MiddlewareContext
from agent_core.protocol import RunContext
from agent_core.runtime.service import AgentRuntime
from agent_core.runtime.store import MemoryRunStore
from agent_core.storage import (
    MemoryWorkflowExecutionStore,
    StorageBundle,
    WorkflowIdempotencyRecord,
)
from agent_core.storage.runtime import RuntimeConcurrencyError
from agent_core.tasks import MemoryTaskQueue, TaskEnvelope
from agent_core.tools import ToolExecutor, ToolRegistry
from agent_core.workflow.durable import DurableWorkflowRunner, WorkflowNodeError
from agent_core.workflow.state_machine import StateMachineBuilder


class _LifecycleStore:
    def __init__(self) -> None:
        self.initialized = 0
        self.closed = 0

    async def initialize(self) -> None:
        self.initialized += 1

    async def close(self) -> None:
        self.closed += 1


def _bundle_with(store: Any) -> StorageBundle:
    return StorageBundle(
        env="test",
        sqlite_path="",
        postgres_url=None,
        require_access=False,
        session_store=store,
        runtime_store=store,
        context_store=store,
        memory_store=store,
        model_selection_store=store,
        run_lease_store=store,
        workflow_execution_store=store,
        workflow_idempotency_store=store,
        session_event_store=store,
        knowledge_graph_store=store,
    )


@pytest.mark.asyncio
async def test_storage_bundle_calls_async_lifecycle_hooks() -> None:
    store = _LifecycleStore()
    bundle = _bundle_with(store)

    await bundle.initialize()
    await bundle.close()

    assert store.initialized == 10
    assert store.closed == 10


@pytest.mark.asyncio
async def test_workflow_retryable_error_enters_waiting_retry() -> None:
    async def unavailable(_state: dict[str, Any]) -> dict[str, Any]:
        raise WorkflowNodeError("上游连接暂时不可用", retryable=True)

    machine = (
        StateMachineBuilder()
        .add_node("unavailable", unavailable)
        .add_edge("__start__", "unavailable")
        .add_edge("unavailable", "__end__")
        .build()
    )
    runner = DurableWorkflowRunner(
        machine,
        MemoryWorkflowExecutionStore(),
        raise_on_failure=False,
        max_retries=1,
    )

    execution = await runner.start("retryable", {})

    assert execution.status == "waiting_retry"
    assert execution.result_data["workflow_retry_count"] == 1


class _ConflictingIdempotencyStore:
    async def reserve(self, record: WorkflowIdempotencyRecord) -> WorkflowIdempotencyRecord:
        return WorkflowIdempotencyRecord(
            key=record.key,
            workflow_id=record.workflow_id,
            execution_id="other-execution",
            node_id=record.node_id,
            arguments_hash=record.arguments_hash,
        )

    async def complete(self, key: str, *, result: dict[str, Any], message: str = "") -> None:
        del key, result, message

    async def fail(self, key: str, *, error: str, message: str = "") -> None:
        del key, error, message


@pytest.mark.asyncio
async def test_workflow_idempotency_conflict_blocks_node_execution() -> None:
    calls = 0

    async def side_effect(_state: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    machine = (
        StateMachineBuilder()
        .add_node("side_effect", side_effect)
        .add_edge("__start__", "side_effect")
        .add_edge("side_effect", "__end__")
        .build()
    )
    runner = DurableWorkflowRunner(
        machine,
        MemoryWorkflowExecutionStore(),
        raise_on_failure=False,
        idempotency_store=_ConflictingIdempotencyStore(),
    )

    execution = await runner.start("conflict", {})

    assert execution.status == "failed"
    assert calls == 0


@pytest.mark.asyncio
async def test_stream_rejects_duplicate_idempotency_key() -> None:
    store = MemoryRunStore()
    existing = RunContext(thread_id="thread", run_id="existing", idempotency_key="same-key")
    await store.save_run(existing)
    runtime = AgentRuntime(object(), run_store=store)  # type: ignore[arg-type]

    with pytest.raises(RuntimeConcurrencyError):
        async for _chunk in runtime.stream("thread", "再次请求", idempotency_key="same-key"):
            pass


@pytest.mark.asyncio
async def test_hitl_persists_waiting_approval_state() -> None:
    queue = ApprovalQueue()
    middleware = HumanInTheLoopMiddleware(
        queue=queue,
        approval_needed=["dangerous"],
        timeout_seconds=5,
    )
    runtime_context = RunContext(thread_id="thread", run_id="run", status="running")
    snapshots: list[tuple[str, bool]] = []

    async def persist(_state: dict[str, Any]) -> None:
        snapshots.append((runtime_context.status, runtime_context.pending_approval is not None))

    runtime_context.checkpoint_callback = persist
    context = MiddlewareContext(
        tool_name="dangerous",
        tool_args={"value": 1},
        metadata={"thread_id": "thread", "run_id": "run"},
        run_context=runtime_context,
    )
    pending = asyncio.create_task(middleware.before_tool(context))
    for _ in range(100):
        if runtime_context.pending_approval is not None:
            break
        await asyncio.sleep(0)

    assert runtime_context.status == "waiting_approval"
    assert runtime_context.pending_approval is not None
    approval_id = runtime_context.pending_approval.approval_id
    assert await queue.approve(approval_id) is True

    result = await pending

    assert result.action == MiddlewareAction.CONTINUE
    assert runtime_context.status == "running"
    assert runtime_context.pending_approval is None
    assert ("waiting_approval", True) in snapshots
    assert snapshots[-1] == ("running", False)


@pytest.mark.asyncio
async def test_tool_executor_limits_direct_and_batch_calls() -> None:
    active = 0
    peak = 0

    async def slow(value: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return value

    registry = ToolRegistry()
    registry.register("slow", slow, "慢工具")
    executor = ToolExecutor(registry, max_concurrent=2)

    await asyncio.gather(
        *(executor.execute_result("slow", {"value": index}) for index in range(6))
    )

    assert peak <= 2


@pytest.mark.asyncio
async def test_memory_task_queue_supports_configured_consumer_concurrency() -> None:
    queue = MemoryTaskQueue(concurrency=2)
    active = 0
    peak = 0
    received: list[str] = []

    async def handler(envelope: TaskEnvelope) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        received.append(envelope.intent)
        active -= 1

    consumer = asyncio.create_task(queue.consume(handler))
    for index in range(4):
        await queue.publish(TaskEnvelope(intent=f"task-{index}"))
    await queue._queue.join()
    await queue.close()
    await consumer

    assert len(received) == 4
    assert peak == 2
