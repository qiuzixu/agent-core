"""面向 HTTP/前端适配器的通用运行时值对象和消息解析工具。

这里不包含 FastAPI、LangGraph 或业务编排，只处理跨 Agent 都相同的 thread/run
记录和输入消息解析。具体 API 协议仍由应用层决定。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_core.protocol.messages import Message
from agent_core.protocol.runtime import RunContext


def now_iso() -> str:
    """返回 UTC ISO 时间。"""
    return datetime.now(UTC).isoformat()


def message_json(message: Message) -> dict[str, Any]:
    """把 Core 消息转换为前端可消费的 OpenAI 兼容字典。"""
    return message.to_openai_dict()


@dataclass
class ThreadRecord:
    """通用 thread 运行摘要。"""

    thread_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    status: str = "idle"
    interrupt: dict[str, Any] | None = None
    pending_prompt: str | None = None
    user_id: str = "anonymous"
    tenant_id: str = "default"


@dataclass
class RunRecord:
    """通用后台运行句柄。"""

    run_id: str
    thread_id: str
    task: asyncio.Task[dict[str, Any]]
    context: RunContext
    status: str = "running"
    result: dict[str, Any] | None = None
    error: str | None = None


def content_text(content: Any) -> str:
    """兼容字符串消息和常见文本块数组。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text", block.get("content"))
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def extract_prompt(value: Any) -> str:
    """从 thread/run 输入对象中提取最后一条用户消息。"""
    if not isinstance(value, dict):
        return ""
    messages = value.get("messages")
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        if item.get("role") not in {"user", "human"}:
            continue
        text = content_text(item.get("content"))
        if text:
            return text
    return ""


__all__ = [
    "RunRecord",
    "ThreadRecord",
    "content_text",
    "extract_prompt",
    "message_json",
    "now_iso",
]
