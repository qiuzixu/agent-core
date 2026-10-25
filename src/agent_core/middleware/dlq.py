"""死信队列（DLQ）中间件。

依赖 ports.DeadLetterSink 协议写入死信；agent-core 内置 JSONL 文件实现，
业务层可注入 RabbitMQ dead-letter exchange、Redis Stream 等。

配置：
    AGENT_DLQ_ENABLED: 是否启用（默认 true）
    AGENT_DLQ_PATH: 内置 JSONL 路径（默认 ./dlq.jsonl）
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)
from agent_core.ports.platform import DeadLetterRecord, DeadLetterSink

logger = logging.getLogger(__name__)

__all__ = ["DLQConfig", "DLQMiddleware", "JsonlDeadLetterSink"]


@dataclass
class DLQConfig:
    enabled: bool = True


class JsonlDeadLetterSink:
    """内置默认死信输出：追加写 JSONL 文件。"""

    def __init__(self, path: str = "./dlq.jsonl") -> None:
        self._path = path

    async def push(self, record: DeadLetterRecord) -> None:
        data = {
            "timestamp": record.timestamp,
            "tool_name": record.tool_name,
            "args_summary": record.args_summary,
            "error": record.error,
            "session_id": record.session_id,
            "user_id": record.user_id,
        }
        try:
            p = Path(self._path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("[DLQ] 写入死信失败")


class DLQMiddleware(Middleware):
    priority = 41
    """工具调用失败时通过 DeadLetterSink 写入死信。"""

    def __init__(
        self,
        config: DLQConfig | None = None,
        sink: DeadLetterSink | None = None,
    ) -> None:
        self._config = config or DLQConfig()
        self._sink = sink or JsonlDeadLetterSink()

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        if context.tool_error is None:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        tool_name = context.tool_call.get("name", "unknown") if context.tool_call else "unknown"
        args = str(context.tool_call.get("args", ""))[:500] if context.tool_call else ""
        record = DeadLetterRecord(
            timestamp=time.time(),
            tool_name=tool_name,
            args_summary=args,
            error=str(context.tool_error)[:500],
            session_id=getattr(context, "session_id", "") or "",
            user_id=getattr(context, "user_id", "") or "",
        )
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.create_task(self._sink.push(record))
        except RuntimeError:
            import asyncio as _aio
            _aio.get_event_loop().run_until_complete(self._sink.push(record))

        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
