"""tasks 模块升级点测试。

覆盖：
1. Worker per-intent 超时与并发信号量。
2. ``worker_queue_names`` Worker 角色队列解析。
3. 调度器僵尸任务回收与重试入队的事件记录（审计链补全）。
4. RabbitMQ 毒消息（信封解析失败）的指标记录与对账标识。
5. TaskEnvelope pydantic 兼容接口的 JSON 安全序列化。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_core.tasks import (
    AgentTask,
    AgentTaskRetryScheduler,
    AgentTaskWorker,
    MemoryAgentTaskStore,
    MemoryTaskQueue,
    TaskEnvelope,
    TaskTimeoutError,
    default_task_metrics,
    worker_queue_names,
)
from agent_core.tasks.queue import RabbitMQTaskQueue


# ========== Worker：per-intent 超时与并发 ==========


async def test_worker_per_intent_timeout() -> None:
    """timeout_seconds_by_intent 对指定 intent 生效，其余走默认（无超时）。"""

    async def handler(envelope: TaskEnvelope) -> dict[str, object]:
        if envelope.intent == "slow.job":
            await asyncio.sleep(0.5)
        return {"ok": True}

    worker = AgentTaskWorker(
        MemoryTaskQueue(),
        handler,
        timeout_seconds_by_intent={"slow.job": 0.1},
    )
    slow = TaskEnvelope(task_id="t-slow", intent="slow.job")
    with pytest.raises(TaskTimeoutError):
        await worker._process(slow)

    fast = TaskEnvelope(task_id="t-fast", intent="fast.job")
    result = await worker._process(fast)
    assert result == {"ok": True}


async def test_worker_per_intent_concurrency() -> None:
    """concurrency_by_intent 控制同 intent 并发上限，不影响其他 intent。"""
    active = {"heavy": 0, "light": 0}
    max_active = {"heavy": 0, "light": 0}

    async def handler(envelope: TaskEnvelope) -> dict[str, object]:
        kind = "heavy" if envelope.intent.startswith("heavy") else "light"
        active[kind] += 1
        max_active[kind] = max(max_active[kind], active[kind])
        await asyncio.sleep(0.05)
        active[kind] -= 1
        return {"ok": True}

    worker = AgentTaskWorker(
        MemoryTaskQueue(),
        handler,
        concurrency=1,
        concurrency_by_intent={"heavy.job": 2},
    )
    envelopes = [TaskEnvelope(task_id=f"t-{i}", intent="heavy.job") for i in range(4)]
    envelopes.append(TaskEnvelope(task_id="t-light", intent="light.job"))
    await asyncio.gather(*(worker._process(envelope) for envelope in envelopes))

    assert max_active["heavy"] == 2
    assert max_active["light"] == 1


# ========== Worker 角色队列解析 ==========


def test_worker_queue_names_explicit_config_wins() -> None:
    """显式配置 task_worker_queues 时直接使用，忽略角色推导。"""
    settings = {"task_worker_queues": ["q-a", "q-b"], "task_queue_name": "base"}
    assert worker_queue_names(settings) == ["q-a", "q-b"]


def test_worker_queue_names_roles() -> None:
    routes = {"workflow.run": "agent.task.heavy", "notify.send": "agent.task.notify"}
    base = {"task_queue_name": "agent.task.normal", "task_queue_intent_routes": routes}

    assert worker_queue_names({**base, "task_worker_role": "all"}) == [
        "agent.task.normal",
        "agent.task.heavy",
        "agent.task.notify",
    ]
    assert worker_queue_names({**base, "task_worker_role": "normal"}) == ["agent.task.normal"]
    assert worker_queue_names({**base, "task_worker_role": "heavy"}) == ["agent.task.heavy"]
    assert worker_queue_names({**base, "task_worker_role": "notify"}) == ["agent.task.notify"]
    # 未知角色回退到全部队列。
    assert worker_queue_names({**base, "task_worker_role": "unknown"}) == [
        "agent.task.normal",
        "agent.task.heavy",
        "agent.task.notify",
    ]


def test_worker_queue_names_role_fallback_to_base() -> None:
    """角色对应队列不存在时回退到基础队列。"""
    settings = {
        "task_queue_name": "agent.task.normal",
        "task_queue_intent_routes": {"workflow.run": "agent.task.heavy"},
        "task_worker_role": "notify",
    }
    assert worker_queue_names(settings) == ["agent.task.normal"]


# ========== 调度器事件记录 ==========


async def test_scheduler_emits_reclaimed_and_retry_events() -> None:
    """租约过期任务被回收并重新入队时，应发出 reclaimed + retry_scheduled 事件。"""
    store = MemoryAgentTaskStore()
    task = await store.create(AgentTask(intent="demo.job", max_retries=3))
    await store.claim(task.task_id, "worker-a", lease_seconds=0.1)
    await asyncio.sleep(0.2)  # 等租约过期，模拟 Worker 崩溃。

    events: list[tuple[str, str]] = []

    async def recorder(envelope: TaskEnvelope, event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, envelope.task_id))

    scheduler = AgentTaskRetryScheduler(
        queue=MemoryTaskQueue(),
        task_store=store,
        worker_id="scheduler-1",
        event_recorder=recorder,
    )
    resumed = await scheduler.resume_due_retries()

    assert resumed == 1
    event_types = [event[0] for event in events]
    assert "agent_task_reclaimed" in event_types
    assert "agent_task_retry_scheduled" in event_types
    # 同一任务的两个事件都带上 task_id，审计链可追溯。
    assert {event[1] for event in events} == {task.task_id}
    updated = await store.get(task.task_id)
    assert updated is not None
    assert updated.status == "queued"
    assert updated.worker_id == "scheduler-1"


async def test_scheduler_without_recorder_still_works() -> None:
    """未注入 event_recorder 时调度器保持原有行为（向后兼容）。"""
    store = MemoryAgentTaskStore()
    task = await store.create(AgentTask(intent="demo.job"))
    await store.claim(task.task_id, "worker-a", lease_seconds=0.1)
    await asyncio.sleep(0.2)

    scheduler = AgentTaskRetryScheduler(
        queue=MemoryTaskQueue(),
        task_store=store,
        worker_id="scheduler-1",
    )
    assert await scheduler.resume_due_retries() == 1


# ========== RabbitMQ 毒消息处理 ==========


class _FakeMessage:
    """记录 reject/ack 调用的假消息。"""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.rejects: list[bool] = []
        self.acks: list[bool] = []

    async def reject(self, *, requeue: bool) -> None:
        self.rejects.append(requeue)

    async def ack(self) -> None:
        self.acks.append(True)


async def test_poison_message_increments_metric_and_rejects() -> None:
    """信封解析失败应计入毒消息指标并 reject 进物理 DLQ。"""
    before = default_task_metrics.snapshot().get("task_poison_message_total", 0)
    queue = RabbitMQTaskQueue(url="amqp://guest:guest@localhost:5672/%2F")
    message = _FakeMessage(b"not-json{{{")
    await queue._handle_poison_message(message)

    after = default_task_metrics.snapshot()["task_poison_message_total"]
    assert after == before + 1
    assert message.rejects == [False]
    assert message.acks == []


def test_peek_task_id_variants() -> None:
    """_peek_task_id 对合法/缺失/损坏消息体分别返回对应标识。"""
    assert RabbitMQTaskQueue._peek_task_id(b'{"task_id": "t-1"}') == "t-1"
    assert RabbitMQTaskQueue._peek_task_id(b'{"foo": 1}') == "<unknown>"
    assert RabbitMQTaskQueue._peek_task_id(b"\xff\xfe") == "<unparseable>"


# ========== TaskEnvelope JSON 兼容 ==========


def test_envelope_model_dump_json_mode_is_json_safe() -> None:
    """mode="json" 的序列化结果必须可以直接 json.dumps（无 datetime 等对象）。"""
    envelope = TaskEnvelope(intent="demo", payload={"k": "值"})
    dumped = envelope.model_dump(mode="json")
    json.dumps(dumped, ensure_ascii=False)  # 不抛异常即 JSON 安全
    restored = TaskEnvelope.model_validate_json(envelope.model_dump_json())
    assert restored.payload == {"k": "值"}
    assert restored.intent == "demo"
