"""请求幂等中间件。

防止用户重复提交相同请求（如双击发送）。
基于请求内容哈希 + 时间窗口去重。

配置：
    AGENT_IDEMPOTENCY_ENABLED: 是否启用（默认 true）
    AGENT_IDEMPOTENCY_WINDOW_SECONDS: 去重时间窗口（默认 10s）
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["IdempotencyConfig", "IdempotencyMiddleware"]


@dataclass
class IdempotencyConfig:
    """幂等中间件配置。"""

    enabled: bool = True
    window_seconds: float = 10.0
    # 最大缓存条目数（防止内存泄漏）
    max_entries: int = 10_000


class IdempotencyMiddleware(Middleware):
    priority = 11
    """在 before_model 阶段检查请求是否为重复提交。

    通过请求消息内容计算指纹，在时间窗口内相同指纹视为重复。
    """

    def __init__(self, config: IdempotencyConfig | None = None) -> None:
        self._config = config or IdempotencyConfig()
        # {fingerprint: timestamp}
        self._seen: dict[str, float] = {}

    def _fingerprint(self, context: MiddlewareContext) -> str:
        """根据请求内容计算指纹。"""
        # 取最后一条用户消息 + 会话标识
        last_msg = context.messages[-1] if context.messages else None
        content = ""
        if last_msg:
            content = str(getattr(last_msg, "content", "") or "")
        # 加入 metadata 中的会话 ID（如果有）
        session_id = context.metadata.get("session_id", "")
        raw = f"{session_id}:{content}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cleanup(self) -> None:
        """清理过期的指纹。"""
        now = time.monotonic()
        expired = [
            k
            for k, ts in self._seen.items()
            if now - ts > self._config.window_seconds
        ]
        for k in expired:
            del self._seen[k]
        # 如果还是太多，删除最旧的
        if len(self._seen) > self._config.max_entries:
            sorted_items = sorted(self._seen.items(), key=lambda x: x[1])
            for k, _ in sorted_items[: len(self._seen) - self._config.max_entries]:
                del self._seen[k]

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        fp = self._fingerprint(context)
        self._cleanup()

        now = time.monotonic()
        if fp in self._seen and (now - self._seen[fp]) < self._config.window_seconds:
            logger.warning(
                "[Idempotency] 检测到重复请求 (指纹 %s...)，窗口 %.1fs 内",
                fp[:12],
                self._config.window_seconds,
            )
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error="重复请求，请稍后再试",
                data={"idempotency_rejected": True},
            )

        self._seen[fp] = now
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
