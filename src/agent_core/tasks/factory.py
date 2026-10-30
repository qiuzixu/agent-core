"""根据应用配置创建已配置的任务队列。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .queue import MemoryTaskQueue, RabbitMQTaskQueue, TaskQueue


def _setting(settings: Any, name: str, default: Any) -> Any:
    """从 pydantic 对象或普通 dict 中读取一个配置项。"""
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def worker_queue_names(settings: Any) -> list[str]:
    """解析 Worker 角色消费的持久化业务队列列表。"""
    configured = list(_setting(settings, "task_worker_queues", []) or [])
    if configured:
        return configured
    base = _setting(settings, "task_queue_name", "agent.task.normal")
    routes = dict(_setting(settings, "task_queue_intent_routes", {}) or {})
    all_queues = [base, *sorted(set(routes.values()))]
    role = _setting(settings, "task_worker_role", "all")
    if role == "all":
        return all_queues
    if role == "normal":
        return [base]
    if role == "heavy":
        heavy = [name for name in routes.values() if name.endswith(".heavy")]
        return heavy or [base]
    if role == "notify":
        notify = [name for name in routes.values() if name.endswith(".notify")]
        return notify or [base]
    return all_queues


def create_task_queue_from_settings(settings: Any) -> TaskQueue:
    backend = _setting(settings, "task_queue_backend", "memory")
    if backend == "memory":
        return MemoryTaskQueue()
    if backend == "rabbitmq":
        url = _setting(settings, "rabbitmq_url", None)
        if not url:
            raise ValueError("rabbitmq_url is required when task_queue_backend is rabbitmq")
        return RabbitMQTaskQueue(
            url=url,
            queue_name=_setting(settings, "task_queue_name", "agent.task.normal"),
            retry_queue_name=_setting(settings, "task_queue_retry_name", "agent.task.retry"),
            dlq_name=_setting(settings, "task_queue_dlq_name", "agent.task.dlq"),
            prefetch=_setting(settings, "task_queue_prefetch", 10),
            max_retries=_setting(settings, "task_queue_max_retries", 3),
            retry_delay_seconds=_setting(settings, "task_queue_retry_delay_seconds", 5.0),
            intent_routes=_setting(settings, "task_queue_intent_routes", {}),
            queue_names=worker_queue_names(settings),
        )
    raise ValueError(f"unsupported task queue backend: {backend}")


_queue: TaskQueue | None = None


def get_task_queue(settings: Any) -> TaskQueue:
    """返回进程级单例队列，复用 RabbitMQ 连接。"""
    global _queue
    if _queue is None:
        _queue = create_task_queue_from_settings(settings)
    return _queue

