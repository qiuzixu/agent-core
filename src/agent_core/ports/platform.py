"""平台工程端口：配置中心 / 告警 / 工具缓存 / 死信队列。

这些端口让 agent-core 的中间件只依赖 Protocol，
具体实现（Apollo / httpx / Redis / RabbitMQ）由业务层按需接入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ConfigCenter",
    "AlertPayload",
    "AlertSink",
    "DeadLetterRecord",
    "DeadLetterSink",
    "ToolCacheStore",
]


# ---------------------------------------------------------------------------
# 配置中心
# ---------------------------------------------------------------------------

@runtime_checkable
class ConfigCenter(Protocol):
    """动态配置中心协议。

    业务层可以选择 Apollo / Consul / Nacos / etcd 等实现，
    agent-core 只要求最小的 get / data / on_change 能力。
    """

    def get(self, key: str, default: Any = None) -> Any: ...

    @property
    def data(self) -> dict[str, Any]:
        """返回全部配置项快照。"""
        ...

    def on_change(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """注册配置变更回调（可选实现，默认 no-op）。"""
        ...


# ---------------------------------------------------------------------------
# 告警
# ---------------------------------------------------------------------------

@dataclass
class AlertPayload:
    """一条告警消息。"""

    key: str
    message: str
    severity: str = "warning"
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AlertSink(Protocol):
    """告警输出端口。

    业务层可以实现为 httpx webhook（飞书/钉钉）、Prometheus Pushgateway、
    邮件等，agent-core 只关心"把告警发出去"。
    """

    async def send(self, alert: AlertPayload) -> None:
        """发送一条告警。实现应自行处理超时和重试。"""
        ...


# ---------------------------------------------------------------------------
# 死信队列
# ---------------------------------------------------------------------------

@dataclass
class DeadLetterRecord:
    """一条死信记录。"""

    timestamp: float
    tool_name: str
    args_summary: str
    error: str
    session_id: str = ""
    user_id: str = ""


@runtime_checkable
class DeadLetterSink(Protocol):
    """死信队列输出端口。

    业务层可以实现为 RabbitMQ dead-letter exchange、Redis Stream、
    或最简单的 JSONL 文件。
    """

    async def push(self, record: DeadLetterRecord) -> None:
        """将一条死信记录写入队列/文件。"""
        ...


# ---------------------------------------------------------------------------
# 工具结果缓存
# ---------------------------------------------------------------------------

@runtime_checkable
class ToolCacheStore(Protocol):
    """工具结果缓存协议。

    可以直接复用 agent_core.cache.base.CacheBackend，
    也可以实现更丰富的语义（如 Redis + TTL）。
    """

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None) -> None: ...

    async def delete(self, key: str) -> bool: ...
