"""全链路追踪：trace_id 生成、W3C traceparent 解析与中间件挂载。

Ports & Adapters：本模块只负责 trace 标识的生成与传播（纯标准库），
完整的 OTEL Span 上报由业务层通过 opentelemetry 适配器实现（可选依赖）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

W3C_TRACEPARENT_HEADER = "traceparent"


def generate_trace_id() -> str:
    """生成 32 位十六进制 trace_id（W3C 兼容）。"""
    return uuid.uuid4().hex


def generate_span_id() -> str:
    """生成 16 位十六进制 span_id（W3C 兼容）。"""
    return uuid.uuid4().hex[:16]


def extract_trace_id(headers: Any) -> str | None:
    """从 HTTP 头提取 trace_id。

    优先 ``X-Trace-Id``；其次解析 W3C ``traceparent``
    （``00-<trace_id>-<span_id>-<flags>``，取 32 位 trace_id 段）。
    """
    if headers is None:
        return None
    get = getattr(headers, "get", None)
    if get is None:
        return None
    raw = get("x-trace-id") or get("X-Trace-Id")
    if raw:
        value = str(raw).strip()
        if value:
            return value[:64]
    traceparent = get(W3C_TRACEPARENT_HEADER) or get("X-Traceparent")
    if traceparent:
        parts = str(traceparent).strip().split("-")
        if len(parts) >= 3 and len(parts[1]) == 32:
            return parts[1]
    return None


def format_traceparent(trace_id: str, span_id: str | None = None) -> str:
    """把 trace_id 包装成 W3C traceparent（便于与 OTEL 生态互通）。"""
    return f"00-{trace_id}-{span_id or generate_span_id()}-01"


@dataclass
class TraceConfig:
    """追踪配置。"""

    enabled: bool = True
    header_name: str = "X-Trace-Id"


class TraceMiddleware(Middleware):
    priority = 30
    """确保每次运行携带 trace_id，并贯穿 model / tool 的中间件上下文。"""

    def __init__(self, config: TraceConfig | None = None) -> None:
        self._config = config or TraceConfig()

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        trace_id = str(context.metadata.get("trace_id") or "").strip()
        if not trace_id:
            trace_id = generate_trace_id()
        context.metadata["trace_id"] = trace_id
        logger.debug("trace_id=%s 进入模型调用（iteration=%s）", trace_id, context.iteration)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 工具上下文的 metadata 由模型上下文拷贝而来，这里兜底补齐。
        context.metadata.setdefault("trace_id", generate_trace_id())
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


__all__ = [
    "TraceConfig",
    "TraceMiddleware",
    "W3C_TRACEPARENT_HEADER",
    "extract_trace_id",
    "format_traceparent",
    "generate_span_id",
    "generate_trace_id",
]