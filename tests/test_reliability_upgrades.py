"""租约、事件和编排上下文升级的回归测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agent_core.callbacks import CallbackManager
from agent_core.protocol.messages import Message, assistant_message
from agent_core.protocol.runtime import RunContext
from agent_core.runtime import AgentRuntime, PlanExecuteAgent
from agent_core.storage import MemoryEventOutbox, MemoryToolOperationStore
from agent_core.tools import ToolExecutor, ToolRegistry
from agent_core.tasks import (
    AgentTask,
    AgentTaskWorker,
    MemoryAgentTaskStore,
    MemoryTaskQueue,
    TaskEnvelope,
    TaskLeaseLostError,
)


class _Model:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        del messages, tools, temperature, max_tokens
        return assistant_message(self.responses.pop(0))


class _Executor:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def run(self, user_input: str, **kwargs: Any) -> str:
        del kwargs
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return user_input


async def test_fencing_token_rejects_late_worker_write() -> None:
    store = MemoryAgentTaskStore()
    task = await store.create(AgentTask(intent="demo"))
    first = await store.claim(task.task_id, "worker-a", lease_seconds=0.01)
    assert first is not None
    await asyncio.sleep(0.15)
    reclaimed = await store.reclaim_stale(limit=1)
    assert len(reclaimed) == 1
    await store.reschedule(task.task_id)
    second = await store.claim(task.task_id, "worker-b", lease_seconds=1.0)
    assert second is not None
    assert second.fencing_token > first.fencing_token
    assert (
        await store.complete(
            task.task_id,
            worker_id="worker-a",
            result={"old": True},
            fencing_token=first.fencing_token,
        )
        is None
    )
    completed = await store.complete(
        task.task_id,
        worker_id="worker-b",
        result={"new": True},
        fencing_token=second.fencing_token,
    )
    assert completed is not None
    assert completed.result == {"new": True}


async def test_worker_rejects_completion_after_lease_loss() -> None:
    class LeaseLostStore(MemoryAgentTaskStore):
        async def complete(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return None

    store = LeaseLostStore()
    task = await store.create(AgentTask(intent="demo"))
    envelope = TaskEnvelope(task_id=task.task_id, intent="demo")
    await store.set_attempt(task.task_id, envelope.attempt_id)
    worker = AgentTaskWorker(
        MemoryTaskQueue(),
        lambda _envelope: _answer(),
        task_store=store,
        worker_id="worker-a",
    )

    async def _run() -> dict[str, Any]:
        return {"ok": True}

    async def _answer() -> dict[str, Any]:
        return await _run()

    with pytest.raises(TaskLeaseLostError):
        await worker._process(envelope)


async def test_callback_timeout_isolated() -> None:
    async def slow(_event: Any) -> None:
        await asyncio.sleep(1)

    class Handler:
        async def on_event(self, event: Any) -> None:
            await slow(event)

    manager = CallbackManager([Handler()], handler_timeout_seconds=0.01)
    failures = await manager.dispatch(RunContext(thread_id="t", run_id="r").emit("started"))
    assert len(failures) == 1
    assert isinstance(failures[0].error, TimeoutError)


def test_event_sequence_survives_history_trim() -> None:
    context = RunContext(thread_id="t", run_id="r")
    for index in range(4):
        context.emit("event", index=index)
    context.events = context.events[-2:]
    restored = RunContext.from_dict(json.loads(json.dumps(context.to_dict())))
    restored.emit("event", index=4)
    assert restored.events[-1].sequence == 4


async def test_plan_execute_parallelizes_ready_steps() -> None:
    plan = {
        "objective": "并行执行",
        "strategy": "两个独立步骤",
        "steps": [
            {"id": "a", "title": "A", "instruction": "A"},
            {"id": "b", "title": "B", "instruction": "B"},
        ],
    }
    executor = _Executor()
    result = await PlanExecuteAgent(
        _Model([json.dumps(plan, ensure_ascii=False), "完成"]),
        executor,
        max_parallel_steps=2,
    ).run("并行")
    assert len(result.steps) == 2
    assert executor.max_active == 2


async def test_non_idempotent_tool_reuses_operation_result() -> None:
    calls = 0

    async def charge(**_kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        return "charged"

    registry = ToolRegistry()
    registry.register("charge", charge, "扣款", idempotent=False)
    executor = ToolExecutor(registry, operation_store=MemoryToolOperationStore())
    first = await executor.execute_result("charge", {"amount": 1}, operation_id="run:tool:c1")
    second = await executor.execute_result("charge", {"amount": 1}, operation_id="run:tool:c1")
    assert calls == 1
    assert first.to_dict() == second.to_dict()
    concurrent = await asyncio.gather(
        executor.execute_result("charge", {"amount": 1}, operation_id="run:tool:c2"),
        executor.execute_result("charge", {"amount": 1}, operation_id="run:tool:c2"),
    )
    assert calls == 2
    assert all(item.success for item in concurrent)
    missing = await executor.execute_result("charge", {"amount": 1})
    assert missing.success is False
    assert missing.error_kind == "invalid_arguments"


async def test_event_outbox_only_confirms_after_publish() -> None:
    outbox = MemoryEventOutbox()
    event = RunContext(thread_id="t", run_id="r").emit("started")
    await outbox.append(event)
    assert [item.event_id for item in await outbox.pending("r")] == [event.event_id]
    await outbox.mark_published(event.event_id)
    assert await outbox.pending("r") == []


async def test_runtime_retries_unconfirmed_outbox_event() -> None:
    class FlakySink:
        def __init__(self) -> None:
            self.calls = 0
            self.events: list[str] = []

        async def publish(self, event: Any) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("消息总线暂时不可用")
            self.events.append(event.event_id)

    sink = FlakySink()
    outbox = MemoryEventOutbox()
    runtime = AgentRuntime(object(), event_sink=sink, event_outbox=outbox)
    context = RunContext(thread_id="t", run_id="r")
    event = context.emit("started")
    await runtime._persist(context)
    assert await outbox.pending("r")
    assert await runtime.drain_event_outbox("r") == 1
    assert sink.events == [event.event_id]
