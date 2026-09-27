"""进程内生命周期回调管理器。

Callback 只观察已经发生的事件。需要修改、重试或阻止执行时，应使用 Middleware。
EventSink 负责把事件发往进程外；CallbackManager 负责进程内订阅，两者消费同一个 RunEvent。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_core.protocol.runtime import RunEvent

logger = logging.getLogger(__name__)

CallbackFunction = Callable[[RunEvent], Awaitable[None] | None]


@runtime_checkable
class CallbackHandler(Protocol):
    """生命周期事件处理器协议。"""

    async def on_event(self, event: RunEvent) -> None: ...


class BaseCallbackHandler:
    """可按需继承的空回调基类。"""

    async def on_event(self, event: RunEvent) -> None:
        del event


class CallableCallbackHandler(BaseCallbackHandler):
    """把同步或异步函数包装为 CallbackHandler。"""

    def __init__(
        self,
        callback: CallbackFunction,
        *,
        event_types: Iterable[str] | None = None,
    ) -> None:
        self._callback = callback
        self._event_types = frozenset(event_types or ())

    async def on_event(self, event: RunEvent) -> None:
        if self._event_types and event.event_type not in self._event_types:
            return
        result = self._callback(event)
        if inspect.isawaitable(result):
            await result


@dataclass(frozen=True)
class CallbackFailure:
    """一次回调分发失败，不改变 Agent 主流程。"""

    handler_name: str
    event_type: str
    error: Exception


class CallbackManager:
    """按注册顺序分发生命周期事件并隔离订阅者故障。"""

    def __init__(self, handlers: Iterable[CallbackHandler] | None = None) -> None:
        self._handlers = list(handlers or ())

    @property
    def handlers(self) -> tuple[CallbackHandler, ...]:
        return tuple(self._handlers)

    def add(self, handler: CallbackHandler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def remove(self, handler: CallbackHandler) -> bool:
        try:
            self._handlers.remove(handler)
        except ValueError:
            return False
        return True

    async def dispatch(self, event: RunEvent) -> tuple[CallbackFailure, ...]:
        """向全部订阅者分发事件；单个订阅者失败不会中断其他订阅者。"""

        failures: list[CallbackFailure] = []
        for handler in tuple(self._handlers):
            try:
                await handler.on_event(event)
            except Exception as exc:
                failure = CallbackFailure(
                    handler_name=type(handler).__name__,
                    event_type=event.event_type,
                    error=exc,
                )
                failures.append(failure)
                logger.exception(
                    "生命周期回调失败：handler=%s event=%s",
                    failure.handler_name,
                    event.event_type,
                )
        return tuple(failures)
