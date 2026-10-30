"""任务队列协议 + 内存 / RabbitMQ 双实现。"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .envelope import TaskEnvelope
from .registry import PermanentTaskError

logger = logging.getLogger(__name__)

TaskHandler = Callable[[TaskEnvelope], Awaitable[dict[str, Any] | None]]


class TaskQueue(ABC):
    """最小生产者/消费者协议，可安全替换为 RabbitMQ。"""

    @abstractmethod
    async def publish(self, envelope: TaskEnvelope) -> None: ...

    @abstractmethod
    async def consume(self, handler: TaskHandler) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def ping(self) -> bool:
        """健康探测：确认后端可达（默认健康，供 /health/deps 调用）。"""
        return True


class MemoryTaskQueue(TaskQueue):
    """进程内队列：用于单元测试和单进程开发。"""

    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def publish(self, envelope: TaskEnvelope) -> None:
        if self._closed:
            raise RuntimeError("memory task queue is closed")
        self._queue.put_nowait(envelope.model_dump(mode="json"))

    async def consume(self, handler: TaskHandler) -> None:
        while not self._closed:
            value = await self._queue.get()
            try:
                await handler(TaskEnvelope.model_validate(value))
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        self._closed = True

    async def ping(self) -> bool:
        """内存队列健康 = 未关闭。"""
        return not self._closed


class RabbitMQTaskQueue(TaskQueue):
    """基于 aio-pika 的持久化 normal/retry/DLQ 队列。"""

    def __init__(
        self,
        *,
        url: str,
        queue_name: str = "agent.task.normal",
        retry_queue_name: str = "agent.task.retry",
        dlq_name: str = "agent.task.dlq",
        prefetch: int = 10,
        max_retries: int = 3,
        retry_delay_seconds: float = 5.0,
        intent_routes: Mapping[str, str] | None = None,
        queue_names: Mapping[str, None] | list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self._url = url
        self._queue_name = queue_name
        self._retry_queue_name = retry_queue_name
        self._dlq_name = dlq_name
        self._prefetch = max(1, int(prefetch))
        self._max_retries = max(0, int(max_retries))
        self._retry_delay_seconds = max(0.1, float(retry_delay_seconds))
        self._intent_routes = dict(intent_routes or {})
        selected = (
            set(queue_names or ())
            if queue_names is not None
            else {self._queue_name, *self._intent_routes.values()}
        )
        if self._queue_name not in selected:
            selected.add(self._queue_name)
        self._queue_names = selected
        self._connection: Any | None = None
        self._channel: Any | None = None

    async def ping(self) -> bool:
        """RabbitMQ 健康 = 能建立/复用连接与信道（惰性建连，不投递测试消息）。"""
        await self._ensure_channel()
        return True

    async def _ensure_channel(self) -> tuple[Any, Any]:
        if self._connection is None or self._connection.is_closed:
            import aio_pika

            self._connection = await aio_pika.connect_robust(self._url)
            channel = await self._connection.channel()
            await channel.set_qos(prefetch_count=self._prefetch)
            await channel.declare_queue(self._dlq_name, durable=True)
            await self._ensure_business_queue(channel, self._queue_name)
            await self._ensure_retry_queue(channel, self._queue_name)
            self._channel = channel
        assert self._channel is not None
        return self._connection, self._channel

    def _retry_queue_for(self, business_queue_name: str) -> str:
        """返回业务队列对应的重试队列名。"""
        if business_queue_name == self._queue_name:
            return self._retry_queue_name
        return f"{business_queue_name}.retry"

    async def _ensure_business_queue(self, channel: Any, queue_name: str) -> Any:
        """声明业务队列；失败时死信到对应的重试队列。"""
        return await channel.declare_queue(
            queue_name,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": self._retry_queue_for(queue_name),
            },
        )

    async def _ensure_retry_queue(self, channel: Any, business_queue_name: str) -> Any:
        """声明重试队列；到期后回到对应的业务队列。"""
        retry_queue_name = self._retry_queue_for(business_queue_name)
        return await channel.declare_queue(
            retry_queue_name,
            durable=True,
            arguments={
                "x-message-ttl": int(self._retry_delay_seconds * 1000),
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": business_queue_name,
            },
        )

    def _route_for_intent(self, intent: str) -> str:
        return self._intent_routes.get(intent, self._queue_name)

    @staticmethod
    def _message(envelope: TaskEnvelope) -> Any:
        import aio_pika

        return aio_pika.Message(
            body=json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=envelope.task_id,
            headers={"x-retry-count": envelope.retry_count},
        )

    async def publish(self, envelope: TaskEnvelope) -> None:
        _connection, channel = await self._ensure_channel()
        routing_key = self._route_for_intent(envelope.intent)
        await self._ensure_business_queue(channel, routing_key)
        await self._ensure_retry_queue(channel, routing_key)
        await channel.default_exchange.publish(
            self._message(envelope),
            routing_key=routing_key,
        )

    async def consume(self, handler: TaskHandler) -> None:
        _connection, channel = await self._ensure_channel()
        queues: list[tuple[str, Any]] = []
        for name in sorted(self._queue_names):
            queues.append((name, await self._ensure_business_queue(channel, name)))
            await self._ensure_retry_queue(channel, name)

        async def on_message(message: Any, source_queue_name: str) -> None:
            envelope: TaskEnvelope | None = None
            try:
                envelope = TaskEnvelope.model_validate_json(message.body.decode("utf-8"))
                await handler(envelope)
                await message.ack()
            except Exception as exc:
                logger.exception("task failed: %s", envelope.task_id if envelope else "<invalid>")
                if envelope is None:
                    await message.reject(requeue=False)
                else:
                    await self._retry_or_dead_letter(envelope, message, exc, source_queue_name)

        for source_queue_name, queue in queues:
            await self._consume_queue(queue, source_queue_name, on_message)

    @staticmethod
    async def _consume_queue(queue: Any, source_queue_name: str, on_message: Any) -> None:
        async def consume_message(message: Any) -> None:
            await on_message(message, source_queue_name)

        await queue.consume(consume_message)

    async def _retry_or_dead_letter(
        self,
        envelope: TaskEnvelope,
        message: Any,
        exc: Exception,
        source_queue_name: str,
    ) -> None:
        _connection, channel = await self._ensure_channel()
        if isinstance(exc, PermanentTaskError):
            envelope.payload = {**envelope.payload, "error": str(exc)}
            await channel.default_exchange.publish(
                self._message(envelope),
                routing_key=self._dlq_name,
            )
            await message.ack()
            return
        envelope.retry_count += 1
        if envelope.retry_count <= self._max_retries:
            await channel.default_exchange.publish(
                self._message(envelope),
                routing_key=self._retry_queue_for(source_queue_name),
            )
        else:
            envelope.payload = {**envelope.payload, "error": str(exc)}
            await channel.default_exchange.publish(
                self._message(envelope),
                routing_key=self._dlq_name,
            )
        await message.ack()

    async def reconcile_dlq(
        self,
        handler: Callable[[TaskEnvelope], Awaitable[bool]],
        *,
        limit: int = 100,
    ) -> tuple[int, int]:
        """确认持久化任务状态不再是 dead 的物理 DLQ 消息。

        The handler returns True to acknowledge and remove the message. It
        returns False to leave the message in RabbitMQ for operator review.
        """
        _connection, channel = await self._ensure_channel()
        queue = await channel.get_queue(self._dlq_name)
        acknowledged = 0
        remaining = 0
        for _ in range(max(0, int(limit))):
            message = await queue.get(fail=False)
            if message is None:
                break
            try:
                envelope = TaskEnvelope.model_validate_json(message.body.decode("utf-8"))
                should_ack = await handler(envelope)
            except Exception:
                await message.reject(requeue=True)
                remaining += 1
                break
            if should_ack:
                await message.ack()
                acknowledged += 1
            else:
                await message.reject(requeue=True)
                remaining += 1
                break
        return acknowledged, remaining

    async def close(self) -> None:
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._connection = None
        self._channel = None

