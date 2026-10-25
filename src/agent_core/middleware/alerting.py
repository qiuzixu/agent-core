"""告警中间件。

依赖 ports.AlertSink 协议发送告警；agent-core 内置日志实现（仅写日志），
业务层可注入 httpx webhook（飞书/钉钉）或 Prometheus Pushgateway。

配置：
    AGENT_ALERT_ENABLED: 是否启用（默认 true）
    AGENT_ALERT_ERROR_RATE_THRESHOLD: 错误率阈值（默认 0.3）
    AGENT_ALERT_LATENCY_P99_MS: P99 延迟阈值毫秒（默认 5000）
    AGENT_ALERT_WINDOW_SIZE: 滑动窗口大小（默认 50 次调用）
    AGENT_ALERT_COOLDOWN_SECONDS: 同一告警冷却期（默认 60s）
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)
from agent_core.ports.platform import AlertPayload, AlertSink

logger = logging.getLogger(__name__)

__all__ = ["AlertingConfig", "AlertingMiddleware", "LoggingAlertSink"]


@dataclass
class AlertingConfig:
    """告警配置。"""

    enabled: bool = True
    error_rate_threshold: float = 0.3
    latency_p99_ms: float = 5000.0
    window_size: int = 50
    cooldown_seconds: float = 60.0


class LoggingAlertSink:
    """内置默认告警输出：仅写日志。"""

    async def send(self, alert: AlertPayload) -> None:
        logger.warning("[Alert][%s] %s", alert.severity, alert.message)


class _SlidingWindow:
    """滑动窗口统计器。"""

    __slots__ = ("_max_size", "_latencies")

    def __init__(self, max_size: int = 50) -> None:
        self._max_size = max_size
        self._latencies: deque[tuple[bool, float]] = deque(maxlen=max_size)

    def record(self, latency_ms: float, success: bool) -> None:
        self._latencies.append((success, latency_ms))

    @property
    def error_rate(self) -> float:
        if not self._latencies:
            return 0.0
        failures = sum(1 for ok, _ in self._latencies if not ok)
        return failures / len(self._latencies)

    def p99(self) -> float:
        if not self._latencies:
            return 0.0
        sorted_lat = sorted(lat for _, lat in self._latencies)
        idx = max(0, int(len(sorted_lat) * 0.99) - 1)
        return sorted_lat[idx]


class AlertingMiddleware(Middleware):
    priority = 32
    """告警：滑动窗口内检查错误率和 P99 延迟，通过 AlertSink 发送。"""

    def __init__(
        self,
        config: AlertingConfig | None = None,
        sink: AlertSink | None = None,
    ) -> None:
        self._config = config or AlertingConfig()
        self._sink = sink or LoggingAlertSink()
        self._tool_window = _SlidingWindow(self._config.window_size)
        self._model_window = _SlidingWindow(self._config.window_size)
        self._last_alert_time: dict[str, float] = {}
        self._start: float = 0.0

    def _should_alert(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last_alert_time.get(key, 0)
        if now - last < self._config.cooldown_seconds:
            return False
        self._last_alert_time[key] = now
        return True

    def _fire_alert(self, key: str, message: str, severity: str = "warning") -> None:
        if not self._should_alert(key):
            return
        payload = AlertPayload(key=key, message=message, severity=severity, timestamp=time.time())
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.create_task(self._sink.send(payload))
        except RuntimeError:
            # No running loop — fire synchronously (for sync contexts)
            import asyncio as _aio
            _aio.get_event_loop().run_until_complete(self._sink.send(payload))

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        self._start = time.monotonic()
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        latency = (time.monotonic() - self._start) * 1000 if self._start else 0.0
        success = context.llm_response is not None
        self._model_window.record(latency, success)
        if self._model_window.error_rate > self._config.error_rate_threshold:
            self._fire_alert(
                "model_error_rate",
                f"LLM 错误率 {self._model_window.error_rate:.1%} > {self._config.error_rate_threshold:.0%}",
            )
        p99 = self._model_window.p99()
        if p99 > self._config.latency_p99_ms:
            self._fire_alert(
                "model_latency",
                f"LLM P99 延迟 {p99:.0f}ms > {self._config.latency_p99_ms:.0f}ms",
            )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        self._start = time.monotonic()
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        latency = (time.monotonic() - self._start) * 1000 if self._start else 0.0
        success = context.tool_result is not None
        self._tool_window.record(latency, success)
        if self._tool_window.error_rate > self._config.error_rate_threshold:
            self._fire_alert(
                "tool_error_rate",
                f"工具错误率 {self._tool_window.error_rate:.1%} > {self._config.error_rate_threshold:.0%}",
            )
        p99 = self._tool_window.p99()
        if p99 > self._config.latency_p99_ms:
            self._fire_alert(
                "tool_latency",
                f"工具 P99 延迟 {p99:.0f}ms > {self._config.latency_p99_ms:.0f}ms",
            )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
