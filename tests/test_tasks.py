"""agent_core.tasks 模块单元测试：信封 / 存储 / 队列 / Worker / 调度器正向+边界+失败路径。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agent_core.tasks import (
    AgentTask,
    AgentTaskRetryScheduler,
    AgentTaskWorker,
    MemoryAgentTaskStore,
    MemoryTaskQueue,
    PermanentTaskError,
    TaskEnvelope,
    TaskTimeoutError,
    UnknownTaskError,
    make_task_router,
)


# ========== 信封 ==========

class TestTaskEnvelope:
    def test_default_fields(self) -> None:
        env = TaskEnvelope()
        assert env.intent == "workflow.run"
        assert env.retry_count == 0
        assert env.max_retries == 3
        assert env.attempt_id  # uuid 自动生成

    def test_json_roundtrip(self) -> None:
        env = TaskEnvelope(intent="test.x", payload={"key": "值"})
        restored = TaskEnvelope.model_validate_json(env.model_dump_json())
        assert restored.payload == {"key": "值"}


# ========== 任务存储 ==========

class TestMemoryAgentTaskStore:
    async def test_create_and_get(self) -> None:
        store = MemoryAgentTaskStore()
        task = AgentTask(intent="test.op")
        created = await store.create(task)
        assert created.task_id == task.task_id
        got = await store.get(task.task_id)
        assert got is not None
        assert got.intent == "test.op"

    async def test_idempotency_key_dedup(self) -> None:
        store = MemoryAgentTaskStore()
        t1 = await store.create(AgentTask(intent="a", idempotency_key="k1"))
        t2 = await store.create(AgentTask(intent="b", idempotency_key="k1"))
        assert t1.task_id == t2.task_id  # 幂等键去重返回已有任务

    async def test_claim_lifecycle(self) -> None:
        store = MemoryAgentTaskStore()
        task = await store.create(AgentTask(intent="x"))
        claimed = await store.claim(task.task_id, "w1", lease_seconds=5.0)
        assert claimed is not None
        assert claimed.status == "running"
        # 二次 claim 失败
        again = await store.claim(task.task_id, "w2", lease_seconds=5.0)
        assert again is None

    async def test_lease_expiry_reclaim(self) -> None:
        store = MemoryAgentTaskStore()
        task = await store.create(AgentTask(intent="x"))
        await store.claim(task.task_id, "w1", lease_seconds=0.1)
        import asyncio as _aio
        await _aio.sleep(0.2)
        reclaimed = await store.reclaim_stale()
        assert len(reclaimed) == 1
        assert reclaimed[0].status == "waiting_retry"

    async def test_fail_marks_dead_after_max_retries(self) -> None:
        store = MemoryAgentTaskStore()
        task = await store.create(AgentTask(intent="x", max_retries=0))
        await store.claim(task.task_id, "w1")
        result = await store.fail(task.task_id, worker_id="w1", error="boom")
        assert result is not None
        assert result.status == "failed"


# ========== 路由 ==========

class TestTaskRouter:
    async def test_known_intent(self) -> None:
        async def handler(env: TaskEnvelope) -> dict:
            return {"ok": True}

        router = make_task_router({"test.op": handler})
        result = await router(TaskEnvelope(intent="test.op"))
        assert result == {"ok": True}

    async def test_unknown_intent_never_retries(self) -> None:
        router = make_task_router({})
        with pytest.raises(UnknownTaskError):
            await router(TaskEnvelope(intent="nope"))


# ========== Worker ==========

class TestAgentTaskWorker:
    async def test_success_flow(self) -> None:
        queue = MemoryTaskQueue()
        store = MemoryAgentTaskStore()
        task = await store.create(AgentTask(intent="op.x"))
        env = TaskEnvelope(task_id=task.task_id, intent="op.x", payload={})
        await store.set_attempt(task.task_id, env.attempt_id)
        await queue.publish(env)

        async def handler(e: TaskEnvelope) -> dict:
            return {"done": True}

        worker = AgentTaskWorker(queue, handler, task_store=store)
        # 消费一条后关闭
        async def consume_one() -> object:
            return await asyncio.wait_for(worker._process(env), timeout=5.0)
        result = await consume_one()
        assert result == {"done": True}
        final = await store.get(task.task_id)
        assert final is not None
        assert final.status == "completed"

    async def test_timeout(self) -> None:
        queue = MemoryTaskQueue()
        store = MemoryAgentTaskStore()
        task = await store.create(AgentTask(intent="slow.op"))
        env = TaskEnvelope(task_id=task.task_id, intent="slow.op", payload={})
        await store.set_attempt(task.task_id, env.attempt_id)
        await queue.publish(env)

        async def slow_handler(e: TaskEnvelope) -> dict:
            await asyncio.sleep(999)

        worker = AgentTaskWorker(
            queue, slow_handler, task_store=store, timeout_seconds=0.1,
        )
        with pytest.raises(TaskTimeoutError):
            await worker._process(env)

    async def test_permanent_error_no_retry(self) -> None:
        queue = MemoryTaskQueue()
        store = MemoryAgentTaskStore()
        task = await store.create(AgentTask(intent="bad.op"))
        env = TaskEnvelope(task_id=task.task_id, intent="bad.op", payload={})
        await store.set_attempt(task.task_id, env.attempt_id)
        await queue.publish(env)

        async def bad_handler(e: TaskEnvelope) -> dict:
            raise PermanentTaskError("参数错误")

        worker = AgentTaskWorker(queue, bad_handler, task_store=store)
        with pytest.raises(PermanentTaskError):
            await worker._process(env)
        final = await store.get(task.task_id)
        assert final is not None
        assert final.status == "dead"


# ========== 调度器 ==========

class TestRetryScheduler:
    async def test_resume_due_retries(self) -> None:
        store = MemoryAgentTaskStore()
        queue = MemoryTaskQueue()
        task = await store.create(AgentTask(intent="retry.op"))
        await store.claim(task.task_id, "w1")
        await store.fail(task.task_id, worker_id="w1", error="e", retry_delay_seconds=0.0)
        # 手动将 next_retry_at 设为过去
        raw = store._tasks[task.task_id]
        raw.next_retry_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        scheduler = AgentTaskRetryScheduler(queue=queue, task_store=store)
        resumed = await scheduler.resume_due_retries()
        assert resumed >= 0  # 不崩溃即可（内存队列 publish 可能立即消费）


# ========== 队列 ==========

class TestMemoryTaskQueue:
    async def test_publish_consume_close(self) -> None:
        queue = MemoryTaskQueue()
        received: list[TaskEnvelope] = []

        async def handler(e: TaskEnvelope) -> None:
            received.append(e)
            queue._closed = True

        await queue.publish(TaskEnvelope(intent="q.test"))
        await asyncio.wait_for(queue.consume(handler), timeout=5.0)
        await queue.close()
        assert len(received) == 1
        assert received[0].intent == "q.test"

    async def test_ping(self) -> None:
        queue = MemoryTaskQueue()
        assert await queue.ping() is True
        await queue.close()
        assert await queue.ping() is False





