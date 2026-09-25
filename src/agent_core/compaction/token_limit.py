"""上下文长度兜底中间件。

它负责最后一道硬截断；完整的摘要、spill 和恢复逻辑由
``CompactionMiddleware`` 负责。两者都属于上下文压缩域，不属于基础中间件协议。
"""

from __future__ import annotations

import logging

from agent_core.middleware.base import Middleware, MiddlewareAction, MiddlewareContext, MiddlewareResult
from agent_core.protocol.messages import Message

logger = logging.getLogger(__name__)


class TokenLimitMiddleware(Middleware):
    """防止消息历史超出 token 限制。"""

    def __init__(
        self,
        max_tokens: int = 4000,
        chars_per_token: int = 4,
        keep_messages: int = 10,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须大于 0")
        if chars_per_token <= 0:
            raise ValueError("chars_per_token 必须大于 0")
        if keep_messages < 1:
            raise ValueError("keep_messages 必须大于 0")
        self._max_tokens = max_tokens
        self._chars_per_token = chars_per_token
        self._keep_messages = keep_messages

    def _estimate_tokens(self, messages: list[Message]) -> int:
        total = sum(len(message.content) for message in messages if message.content)
        return total // self._chars_per_token

    def _trim_messages(self, messages: list[Message]) -> list[Message]:
        """保留 system 消息和最近消息，并避免留下孤儿 tool 结果。"""
        system_messages = [message for message in messages if message.role == "system"]
        other_messages = [message for message in messages if message.role != "system"]
        if len(other_messages) <= self._keep_messages:
            return system_messages + other_messages

        start = len(other_messages) - self._keep_messages
        # 工具结果必须和此前的 assistant tool_call 一起保留，否则提供商会拒绝请求。
        while start > 0 and other_messages[start].role == "tool":
            start -= 1
        return system_messages + other_messages[start:]

    async def before_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        estimated = self._estimate_tokens(ctx.messages)
        if estimated <= self._max_tokens:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        trimmed = self._trim_messages(ctx.messages)
        new_estimated = self._estimate_tokens(trimmed)
        logger.warning(
            "[TokenLimit] 消息历史过长 (%d tokens)，裁剪：%d → %d 条，重新估算 %d tokens",
            estimated,
            len(ctx.messages),
            len(trimmed),
            new_estimated,
        )
        ctx.messages = trimmed
        return MiddlewareResult(
            action=MiddlewareAction.MODIFY,
            data={"messages": trimmed, "trimmed_tokens": estimated - new_estimated},
        )


__all__ = ["TokenLimitMiddleware"]
