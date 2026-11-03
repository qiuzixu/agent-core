"""会话存储协议与 Message 行序列化工具。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from agent_core.access import AccessContext
from agent_core.compaction.types import (
    SUMMARY_CLOSE,
    SUMMARY_OPEN,
    SUMMARY_SOURCE,
)
from agent_core.protocol.messages import Message


def _msg_to_row(msg: Message) -> tuple[str, str, str | None, str | None, str]:
    """Message → (role, content, name, tool_call_id, tool_calls_json)。"""
    return (
        msg.role,
        msg.content,
        msg.name,
        msg.tool_call_id,
        json.dumps(msg.tool_calls, ensure_ascii=False) if msg.tool_calls else "[]",
    )


def _extract_summary_body(content: str) -> str:
    """从压缩 checkpoint 消息中提取摘要正文（SUMMARY 标签之间的内容）。"""
    open_index = content.find(SUMMARY_OPEN)
    close_index = content.find(SUMMARY_CLOSE)
    if open_index != -1 and close_index > open_index:
        return content[open_index + len(SUMMARY_OPEN) : close_index].strip()
    # 无标签（历史数据或格式变化）时退回全文，保证上下文不丢
    return content.strip()


# ──────────────────────────────────────────────
# 数据库行 → Message
# ──────────────────────────────────────────────
def _row_to_msg(row: tuple[Any, ...] | dict[str, Any]) -> Message:
    """数据库行 → Message。

    兼容 sqlite3.Row（可按列名访问）和普通 tuple。
    """
    if isinstance(row, dict):
        role, content, name, tool_call_id, tool_calls_json = (
            row["role"],
            row["content"],
            row.get("name"),
            row.get("tool_call_id"),
            row.get("tool_calls", "[]"),
        )
    else:
        # tuple: (role, content, name, tool_call_id, tool_calls)
        role, content, name, tool_call_id, tool_calls_json = row

    tool_calls: list[dict[str, Any]] = []
    if tool_calls_json:
        try:
            tool_calls = json.loads(tool_calls_json)
        except json.JSONDecodeError:
            pass

    return Message(
        role=role,
        content=content,
        name=name,
        tool_call_id=tool_call_id,
        tool_calls=tool_calls,
    )


# ──────────────────────────────────────────────
# 抽象接口
# ──────────────────────────────────────────────


class BaseSessionStore(ABC):
    """会话存储抽象基类。"""

    @abstractmethod
    async def load(self, thread_id: str, *, access: AccessContext | None = None) -> list[Message]:
        """加载指定 thread 的消息历史。"""

    @abstractmethod
    async def save(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        """覆盖保存指定 thread 的消息历史。"""

    @abstractmethod
    async def append(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        """追加消息到指定 thread（比 save 更高效，不需要先 load）。"""

    @abstractmethod
    async def delete(self, thread_id: str, *, access: AccessContext | None = None) -> None:
        """删除指定 thread 的所有消息。"""

    @abstractmethod
    async def list_threads(self, *, access: AccessContext | None = None) -> list[str]:
        """列出所有 thread_id。"""

    @abstractmethod
    async def count(self, thread_id: str, *, access: AccessContext | None = None) -> int:
        """返回指定 thread 的消息数量。"""

    # 统一端口名称，同时保留早期实现中的短方法名。
    async def load_messages(self, thread_id: str, *, access: AccessContext | None = None) -> list[Message]:
        return await self.load(thread_id, access=access)

    async def append_messages(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        await self.append(thread_id, messages, access=access)

    async def replace_messages(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        await self.save(thread_id, messages, access=access)

    async def delete_thread(self, thread_id: str, *, access: AccessContext | None = None) -> None:
        await self.delete(thread_id, access=access)


# ──────────────────────────────────────────────
# 1. 内存后端（测试用）
# ──────────────────────────────────────────────
