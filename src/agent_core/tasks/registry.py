"""任务意图路由与永久失败语义。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .envelope import TaskEnvelope


class PermanentTaskError(RuntimeError):
    """不允许重试的任务失败（参数错误等）。"""


class TaskTimeoutError(RuntimeError):
    """任务超出其意图对应的执行超时。"""


class UnknownTaskError(PermanentTaskError):
    """Worker 收到了不支持的任务意图。"""


TaskHandler = Callable[[TaskEnvelope], Awaitable[dict[str, Any] | None]]


def make_task_router(
    handlers: Mapping[str, TaskHandler],
) -> TaskHandler:
    """按 intent 将信封路由到已注册的 handler；未知 intent 不会重试。"""

    async def route(envelope: TaskEnvelope) -> dict[str, Any] | None:
        handler = handlers.get(envelope.intent)
        if handler is None:
            raise UnknownTaskError(f"unsupported task intent: {envelope.intent}")
        return await handler(envelope)

    return route


__all__ = [
    "PermanentTaskError",
    "TaskHandler",
    "TaskTimeoutError",
    "UnknownTaskError",
    "make_task_router",
]

