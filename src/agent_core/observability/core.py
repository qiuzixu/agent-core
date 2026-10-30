"""零依赖可观测性中间件和进程内统计。

模块职责：
1. ObservabilityMiddleware —— 在 before_model/after_model、before_tool/after_tool
   钩子上为每次 LLM / 工具调用计时，并可选创建 OpenTelemetry span（未安装
   opentelemetry-sdk 时自动降级为纯日志，零依赖哲学）。
2. AgentStats —— 进程级统计注册表（带容量上限的轻量 LRU）：
   - record_llm_call / record_tool_call：单次调用耗时与成败；
   - record_guard_event：护栏拦截/脱敏计数（与 guardrails 中间件的 _emit 事件同源）；
   - add_sink：把每条记录实时转发给外部系统（Prometheus push、日志管道等），
     sink 异常只记日志，绝不影响主流程。

线程/并发模型：统计字典非加锁，但 CPython 下 dict/list 单操作是原子的，
异步单线程事件循环内不会出现竞争；跨进程聚合请通过 sink 转发，而不是共享内存。
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

# 单会话允许登记的最大护栏事件种类数（事件名来自固定集合，64 足够防御异常键）。
_MAX_GUARD_EVENT_KINDS = 64

# 事件 sink 回调类型：接收一条事件字典（含 type / thread_id / 事件负载数据）。
StatsSink = Callable[[dict[str, Any]], None]


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
    # 护栏事件计数：{"guard_input_blocked": 2, "guard_pii_redacted": 1, ...}
    guard_events: dict[str, int] = field(default_factory=dict)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / len(self.llm_records) if self.llm_records else 0.0


class AgentStats:
    """进程级统计注册表；可通过 add_sink 把记录实时转发到外部系统。

    容量控制（防长跑进程内存无限增长）：
    - max_threads：用 dict 插入序 + 重新插入实现轻量 LRU，超限删最老会话；
    - max_records_per_thread：每会话的调用明细列表超限截断头部。
    """

    def __init__(self, *, max_threads: int = 1000, max_records_per_thread: int = 1000) -> None:
        if max_threads <= 0 or max_records_per_thread <= 0:
            raise ValueError("统计容量必须大于 0")
        self._stats: dict[str, ThreadStats] = {}
        self._max_threads = max_threads
        self._max_records_per_thread = max_records_per_thread
        self._sinks: list[StatsSink] = []

    # ── sink 注册 ────────────────────────────────
    def add_sink(self, sink: StatsSink) -> None:
        """注册记录外发回调；每条记录产生时同步调用，异常只记日志。"""
        self._sinks.append(sink)

    def clear_sinks(self) -> None:
        """清空全部 sink（主要用于测试与运行期重配置）。"""
        self._sinks.clear()

    def _notify_sink(self, event: dict[str, Any]) -> None:
        """把一条记录转发给所有 sink；单个 sink 失败不影响其他 sink 和主流程。"""
        if not self._sinks:
            return
        for sink in self._sinks:
            try:
                sink(event)
            except Exception as exc:
                logger.warning("[Obs] 统计 sink 执行失败：%s", exc)

    # ── 会话注册表（轻量 LRU） ─────────────────────
    def get_stats(self, thread_id: str) -> ThreadStats:
        if thread_id not in self._stats:
            if len(self._stats) >= self._max_threads:
                oldest_thread = next(iter(self._stats))
                del self._stats[oldest_thread]
            self._stats[thread_id] = ThreadStats(thread_id=thread_id)
        else:
            # dict 保持插入顺序，用重新插入实现轻量 LRU（刷新新鲜度）。
            self._stats[thread_id] = self._stats.pop(thread_id)
        return self._stats[thread_id]

    # ── 记录入口 ────────────────────────────────
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
        self._notify_sink(
            {
                "type": "llm_call",
                "thread_id": thread_id,
                "iteration": iteration,
                "latency_ms": latency_ms,
                "tool_calls": tool_calls,
            }
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
        if len(stats.tool_records) > self._max_records_per_thread:
            del stats.tool_records[: len(stats.tool_records) - self._max_records_per_thread]
        self._notify_sink(
            {
                "type": "tool_call",
                "thread_id": thread_id,
                "tool_name": tool_name,
                "latency_ms": latency_ms,
                "success": success,
            }
        )

    def record_guard_event(self, thread_id: str, event_type: str) -> None:
        """记录一次护栏动作（拦截/脱敏），供 /observability/stats 展示。"""
        stats = self.get_stats(thread_id)
        if event_type not in stats.guard_events and len(stats.guard_events) >= _MAX_GUARD_EVENT_KINDS:
            # 防御异常键把字典撑大；正常事件名来自固定集合，不会走到这里。
            return
        stats.guard_events[event_type] = stats.guard_events.get(event_type, 0) + 1
        self._notify_sink(
            {
                "type": "guard_event",
                "thread_id": thread_id,
                "event_type": event_type,
            }
        )

    def all_threads(self) -> list[str]:
        return list(self._stats)

    def summary(self) -> dict[str, Any]:
        """聚合快照：每个会话的调用次数、平均耗时与护栏事件计数。"""
        return {
            thread_id: {
                "llm_calls": stats.total_llm_calls,
                "tool_calls": stats.total_tool_calls,
                "avg_latency_ms": round(stats.avg_latency_ms, 1),
                "total_latency_ms": round(stats.total_latency_ms, 1),
                "guard_events": dict(sorted(stats.guard_events.items())),
            }
            for thread_id, stats in self._stats.items()
        }


# 进程级单例：全模块共享，vanilla 的 /observability/stats 直接读取。
agent_stats = AgentStats()


class ObservabilityMiddleware(Middleware):
    """记录模型和工具调用耗时，并可选地创建 OpenTelemetry span。

    span 覆盖两段链路：
    - LLM 调用：before_model 开 span，after_model/handle_exception 收口；
    - 工具调用：before_tool 开 span，after_tool 收口（含成败属性），
      让"这次回复慢是因为哪个工具"在 tracing 里可以直接看出来。
    """

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
        # 工具链路：(开始时间, span)。与模型链路分开两个 ContextVar。
        self._tool_state: ContextVar[tuple[float, Any | None]] = ContextVar(
            f"agent_core_observability_tool_span_{id(self)}",
            default=(0.0, None),
        )
        self._tracer = _get_tracer() if enable_otel else None

    def _context_thread_id(self, ctx: MiddlewareContext) -> str:
        return str(ctx.metadata.get("thread_id") or self._thread_id)

    # ── 模型调用链路 ──────────────────────────────
    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        call_start = time.perf_counter()
        span = None
        if self._tracer:
            span = self._tracer.start_span(f"llm.call.iter{context.iteration}")
            span.set_attribute("iteration", context.iteration)
            span.set_attribute("message_count", len(context.messages))
        self._model_state.set((call_start, span))
        thread_id = self._context_thread_id(context)
        logger.log(
            self._log_level,
            "[Obs] LLM call start: thread=%s iter=%d msgs=%d",
            thread_id,
            context.iteration,
            len(context.messages),
        )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        call_start, span = self._model_state.get()
        latency_ms = (time.perf_counter() - call_start) * 1000 if call_start else 0.0
        response = context.llm_response
        tool_calls = len(response.tool_calls) if response else 0
        thread_id = self._context_thread_id(context)
        agent_stats.record_llm_call(
            thread_id=thread_id,
            iteration=context.iteration,
            latency_ms=latency_ms,
            input_messages=len(context.messages),
            tool_calls=tool_calls,
        )
        logger.log(
            self._log_level,
            "[Obs] LLM call done: thread=%s iter=%d latency=%.0fms content=%d tool_calls=%d",
            thread_id,
            context.iteration,
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

    # ── 工具调用链路 ──────────────────────────────
    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        tool_start = time.perf_counter()
        span = None
        if self._tracer:
            tool_name = context.tool_name or "unknown"
            span = self._tracer.start_span(f"tool.call.{tool_name}")
            span.set_attribute("tool_name", tool_name)
        self._tool_state.set((tool_start, span))
        logger.log(self._log_level, "[Obs] Tool call start: %s args=%s", context.tool_name, context.tool_args)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        tool_start, span = self._tool_state.get()
        latency_ms = (time.perf_counter() - tool_start) * 1000 if tool_start else 0.0
        success = bool(context.metadata.get("tool_success", True))
        agent_stats.record_tool_call(
            thread_id=self._context_thread_id(context),
            tool_name=context.tool_name or "unknown",
            latency_ms=latency_ms,
            success=success,
        )
        logger.log(
            self._log_level,
            "[Obs] Tool call done: %s latency=%.0fms result_len=%d",
            context.tool_name,
            latency_ms,
            len(context.tool_result) if context.tool_result else 0,
        )
        if span:
            span.set_attribute("latency_ms", latency_ms)
            span.set_attribute("success", success)
            span.end()
        self._tool_state.set((0.0, None))
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


def _get_tracer() -> Any:
    """按需获取 OTel tracer，未安装依赖时返回 None（自动降级）。"""
    try:
        from opentelemetry import trace

        return trace.get_tracer("zero-agent-core")
    except ImportError:
        logger.warning("opentelemetry-sdk 未安装，OTel 追踪已禁用")
        return None


def setup_tracing(
    service_name: str = "zero-agent",
    otlp_endpoint: str | None = None,
    console_export: bool = False,
) -> bool:
    """配置可选的 OpenTelemetry 导出器。

    - console_export=True：span 打到控制台（本地调试）；
    - otlp_endpoint：通过 OTLP gRPC 导出到 Jaeger / Tempo 等后端。
    未安装 opentelemetry-sdk 时返回 False，不影响服务启动。
    """
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
