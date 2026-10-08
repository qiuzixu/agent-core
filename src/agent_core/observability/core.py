"""零依赖可观测性中间件和进程内统计。"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
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

    def __init__(self, *, max_threads: int = 1000, max_records_per_thread: int = 1000) -> None:
        if max_threads <= 0 or max_records_per_thread <= 0:
            raise ValueError("统计容量必须大于 0")
        self._stats: dict[str, ThreadStats] = {}
        self._max_threads = max_threads
        self._max_records_per_thread = max_records_per_thread

    def get_stats(self, thread_id: str) -> ThreadStats:
        if thread_id not in self._stats:
            if len(self._stats) >= self._max_threads:
                oldest_thread = next(iter(self._stats))
                del self._stats[oldest_thread]
            self._stats[thread_id] = ThreadStats(thread_id=thread_id)
        else:
            # dict 保持插入顺序，用重新插入实现轻量 LRU。
            self._stats[thread_id] = self._stats.pop(thread_id)
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
        if len(stats.llm_records) > self._max_records_per_thread:
            del stats.llm_records[: len(stats.llm_records) - self._max_records_per_thread]

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
        if len(stats.tool_records) > self._max_records_per_thread:
            del stats.tool_records[: len(stats.tool_records) - self._max_records_per_thread]

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
        # 同一个 Agent 可被多个会话并发复用，计时状态必须按异步任务隔离。
        self._model_state: ContextVar[tuple[float, Any | None]] = ContextVar(
            f"agent_core_observability_model_{id(self)}",
            default=(0.0, None),
        )
        self._tool_start: ContextVar[float] = ContextVar(
            f"agent_core_observability_tool_{id(self)}",
            default=0.0,
        )
        self._tracer = _get_tracer() if enable_otel else None

    def _context_thread_id(self, ctx: MiddlewareContext) -> str:
        return str(ctx.metadata.get("thread_id") or self._thread_id)

    async def before_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        call_start = time.perf_counter()
        span = None
        if self._tracer:
            span = self._tracer.start_span(f"llm.call.iter{ctx.iteration}")
            span.set_attribute("iteration", ctx.iteration)
            span.set_attribute("message_count", len(ctx.messages))
        self._model_state.set((call_start, span))
        thread_id = self._context_thread_id(ctx)
        logger.log(
            self._log_level,
            "[Obs] LLM call start: thread=%s iter=%d msgs=%d",
            thread_id,
            ctx.iteration,
            len(ctx.messages),
        )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        call_start, span = self._model_state.get()
        latency_ms = (time.perf_counter() - call_start) * 1000 if call_start else 0.0
        response = ctx.llm_response
        tool_calls = len(response.tool_calls) if response else 0
        thread_id = self._context_thread_id(ctx)
        agent_stats.record_llm_call(
            thread_id=thread_id,
            iteration=ctx.iteration,
            latency_ms=latency_ms,
            input_messages=len(ctx.messages),
            tool_calls=tool_calls,
        )
        logger.log(
            self._log_level,
            "[Obs] LLM call done: thread=%s iter=%d latency=%.0fms content=%d tool_calls=%d",
            thread_id,
            ctx.iteration,
            latency_ms,
            len(response.content) if response else 0,
            tool_calls,
        )
        if span:
            span.set_attribute("latency_ms", latency_ms)
            span.set_attribute("tool_calls", tool_calls)
            span.end()
        self._model_state.set((0.0, None))
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def handle_exception(self, exc: Exception, ctx: MiddlewareContext) -> bool:
        """关闭失败模型调用的 span；观测中间件本身不决定是否重试。"""
        del ctx
        call_start, span = self._model_state.get()
        if span is not None:
            record_exception = getattr(span, "record_exception", None)
            if record_exception is not None:
                record_exception(exc)
            if call_start:
                span.set_attribute("latency_ms", (time.perf_counter() - call_start) * 1000)
            span.end()
        self._model_state.set((0.0, None))
        return False

    async def before_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        self._tool_start.set(time.perf_counter())
        logger.log(self._log_level, "[Obs] Tool call start: %s args=%s", ctx.tool_name, ctx.tool_args)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, ctx: MiddlewareContext) -> MiddlewareResult:
        tool_start = self._tool_start.get()
        latency_ms = (time.perf_counter() - tool_start) * 1000 if tool_start else 0.0
        agent_stats.record_tool_call(
            thread_id=self._context_thread_id(ctx),
            tool_name=ctx.tool_name or "unknown",
            latency_ms=latency_ms,
            success=bool(ctx.metadata.get("tool_success", True)),
        )
        logger.log(
            self._log_level,
            "[Obs] Tool call done: %s latency=%.0fms result_len=%d",
            ctx.tool_name,
            latency_ms,
            len(ctx.tool_result) if ctx.tool_result else 0,
        )
        self._tool_start.set(0.0)
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
