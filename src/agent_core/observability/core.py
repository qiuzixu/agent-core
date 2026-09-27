"""零依赖可观测性中间件和进程内统计。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)


@dataclass
class CallRecord:
    """单次模型调用记录。"""

    timestamp: float
    iteration: int
    latency_ms: float
    input_messages: int
    tool_calls: int
    finish_reason: str = "unknown"


@dataclass
class ToolRecord:
    """单次工具调用记录。"""

    timestamp: float
    tool_name: str
    latency_ms: float
    success: bool = True


@dataclass
class ThreadStats:
    """单个会话的累计统计。"""

    thread_id: str
    total_llm_calls: int = 0
    total_tool_calls: int = 0
    total_latency_ms: float = 0.0
    llm_records: list[CallRecord] = field(default_factory=list)
    tool_records: list[ToolRecord] = field(default_factory=list)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / len(self.llm_records) if self.llm_records else 0.0


class AgentStats:
    """进程级统计注册表；生产环境可通过事件 sink 转发到外部系统。"""

    def __init__(self) -> None:
        self._stats: dict[str, ThreadStats] = {}

    def get_stats(self, thread_id: str) -> ThreadStats:
        if thread_id not in self._stats:
            self._stats[thread_id] = ThreadStats(thread_id=thread_id)
        return self._stats[thread_id]

    def record_llm_call(
        self,
        thread_id: str,
        iteration: int,
        latency_ms: float,
        input_messages: int,
        tool_calls: int,
        finish_reason: str = "unknown",
    ) -> None:
        stats = self.get_stats(thread_id)
        stats.total_llm_calls += 1
        stats.total_latency_ms += latency_ms
        stats.llm_records.append(
            CallRecord(
                timestamp=time.time(),
                iteration=iteration,
                latency_ms=latency_ms,
                input_messages=input_messages,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        )

    def record_tool_call(
        self,
        thread_id: str,
        tool_name: str,
        latency_ms: float,
        success: bool = True,
    ) -> None:
        stats = self.get_stats(thread_id)
        stats.total_tool_calls += 1
        stats.tool_records.append(
            ToolRecord(
                timestamp=time.time(),
                tool_name=tool_name,
                latency_ms=latency_ms,
                success=success,
            )
        )

    def all_threads(self) -> list[str]:
        return list(self._stats)

    def summary(self) -> dict[str, Any]:
        return {
            thread_id: {
                "llm_calls": stats.total_llm_calls,
                "tool_calls": stats.total_tool_calls,
                "avg_latency_ms": round(stats.avg_latency_ms, 1),
                "total_latency_ms": round(stats.total_latency_ms, 1),
            }
            for thread_id, stats in self._stats.items()
        }


agent_stats = AgentStats()


class ObservabilityMiddleware(Middleware):
    """记录模型和工具调用耗时，并可选地创建 OpenTelemetry span。"""

    def __init__(
        self,
        thread_id: str = "default",
        log_level: int = logging.INFO,
        enable_otel: bool = False,
    ) -> None:
        self._thread_id = thread_id
        self._log_level = log_level
        self._span: Any = None
        self._call_start = 0.0
        self._tool_start = 0.0
        self._tracer = _get_tracer() if enable_otel else None

    async def before_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        self._call_start = time.perf_counter()
        if self._tracer:
            self._span = self._tracer.start_span(f"llm.call.iter{ctx.iteration}")
            self._span.set_attribute("iteration", ctx.iteration)
            self._span.set_attribute("message_count", len(ctx.messages))
        logger.log(
            self._log_level,
            "[Obs] LLM call start: thread=%s iter=%d msgs=%d",
            self._thread_id,
            ctx.iteration,
            len(ctx.messages),
        )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        latency_ms = (time.perf_counter() - self._call_start) * 1000
        response = ctx.llm_response
        tool_calls = len(response.tool_calls) if response else 0
        agent_stats.record_llm_call(
            thread_id=self._thread_id,
            iteration=ctx.iteration,
            latency_ms=latency_ms,
            input_messages=len(ctx.messages),
            tool_calls=tool_calls,
        )
        logger.log(
            self._log_level,
            "[Obs] LLM call done: thread=%s iter=%d latency=%.0fms content=%d tool_calls=%d",
            self._thread_id,
            ctx.iteration,
            latency_ms,
            len(response.content) if response else 0,
            tool_calls,
        )
        if self._span:
            self._span.set_attribute("latency_ms", latency_ms)
            self._span.set_attribute("tool_calls", tool_calls)
            self._span.end()
            self._span = None
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        self._tool_start = time.perf_counter()
        logger.log(self._log_level, "[Obs] Tool call start: %s args=%s", ctx.tool_name, ctx.tool_args)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        latency_ms = (time.perf_counter() - self._tool_start) * 1000
        agent_stats.record_tool_call(
            thread_id=self._thread_id,
            tool_name=ctx.tool_name or "unknown",
            latency_ms=latency_ms,
        )
        logger.log(
            self._log_level,
            "[Obs] Tool call done: %s latency=%.0fms result_len=%d",
            ctx.tool_name,
            latency_ms,
            len(ctx.tool_result) if ctx.tool_result else 0,
        )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


def _get_tracer() -> Any:
    """按需获取 OTel tracer，未安装依赖时返回 None。"""
    try:
        from opentelemetry import trace

        return trace.get_tracer("handwritten-agent-core")
    except ImportError:
        logger.warning("opentelemetry-sdk 未安装，OTel 追踪已禁用")
        return None


def setup_tracing(
    service_name: str = "handwritten-agent",
    otlp_endpoint: str | None = None,
    console_export: bool = False,
) -> bool:
    """配置可选的 OpenTelemetry 导出器。"""
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )
    except ImportError:
        logger.warning("opentelemetry-sdk 未安装，跳过 tracing 配置")
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if console_export:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
            )
        except ImportError:
            logger.warning("OTLP 导出依赖未安装，跳过远端 tracing")
    trace.set_tracer_provider(provider)
    return True


__all__ = [
    "AgentStats",
    "CallRecord",
    "ObservabilityMiddleware",
    "ThreadStats",
    "ToolRecord",
    "agent_stats",
    "setup_tracing",
]
