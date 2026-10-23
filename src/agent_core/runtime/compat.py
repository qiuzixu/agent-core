"""面向 HTTP/前端适配器的通用运行时值对象和消息解析工具。

这里不包含 Web 框架或业务编排，只处理跨 Agent 都相同的 thread/run
记录和输入消息解析。具体 API 协议仍由应用层决定。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from agent_core.protocol.messages import Message
from agent_core.protocol.runtime import RunContext, RunStatus

# ThreadRecord 只描述“当前是否在执行”。等待审批等业务细节由 interrupt/
# pending_prompt 单独表达，避免把 thread 状态做成第二个 Run 状态机。
ThreadStatus = Literal["idle", "running"]


def now_iso() -> str:
    """返回 UTC ISO 时间。"""
    return datetime.now(UTC).isoformat()


def message_json(message: Message) -> dict[str, Any]:
    """把 Core 消息转换为前端可消费的 OpenAI 兼容字典。"""
    return message.to_openai_dict()


def messages_json(messages: Iterable[Message]) -> list[dict[str, Any]]:
    """批量把 Core 消息转换为 OpenAI 兼容字典。"""
    return [message_json(message) for message in messages]


@dataclass
class ThreadRecord:
    """通用 thread 运行摘要。

    这个对象是进程内 API 状态，不负责持久化；应用层需要落库时自行投影。
    """

    thread_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    status: ThreadStatus = "idle"
    # interrupt/pending_prompt 用于前端展示当前是否等待人工确认。
    interrupt: dict[str, Any] | None = None
    pending_prompt: str | None = None
    user_id: str = "anonymous"
    tenant_id: str = "default"

    def touch(self) -> None:
        """刷新更新时间；所有状态变化都应经由具体 mark_/set_ 方法。"""
        self.updated_at = now_iso()

    def mark_running(self) -> None:
        """标记 thread 正在执行 run。"""
        self.status = "running"
        self.touch()

    def mark_idle(self) -> None:
        """标记 thread 当前没有活跃 run。"""
        self.status = "idle"
        self.touch()

    def set_interrupt(
        self,
        interrupt: dict[str, Any] | None,
        pending_prompt: str | None = None,
    ) -> None:
        """设置或清除人工确认上下文。

        interrupt 为 None 时表示恢复/取消审批完成，应同时清空待续跑提示词。
        """
        self.interrupt = interrupt
        self.pending_prompt = None if interrupt is None else pending_prompt
        self.touch()

    def to_dict(self) -> dict[str, Any]:
        """返回线程摘要的 JSON 兼容结构。"""
        return {
            "thread_id": self.thread_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "interrupt": self.interrupt,
            "pending_prompt": self.pending_prompt,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
        }


@dataclass
class RunRecord:
    """通用后台运行句柄。

    task 是进程内 asyncio 对象，只能由运行层调度；对外 API 应返回
    to_summary()，避免把不可序列化的执行句柄泄露到前端。
    """

    run_id: str
    thread_id: str
    task: asyncio.Task[dict[str, Any]]
    context: RunContext
    status: RunStatus = "running"
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_summary(self) -> dict[str, Any]:
        """返回可安全暴露给 API 的 run 摘要，不包含 asyncio.Task。"""
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }


def content_text(content: Any) -> str:
    """兼容字符串消息和常见文本块数组，提取纯文本。

    这里只提取文本；图片、音频等非文本块会被忽略，避免把 base64
    或二进制占位内容注入到 prompt 中。
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    text_types = {"text", "input_text", "output_text"}
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        # 标准 OpenAI 风格显式携带 type；没有 type 时退回旧格式 text/content。
        block_type = block.get("type")
        if block_type is not None and block_type not in text_types:
            continue
        text = block.get("text", block.get("content"))
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def extract_prompt(value: Any) -> str:
    """从 thread/run 输入对象中提取最后一条用户消息。

    支持三种常见形状：
    1. 纯字符串输入；
    2. {"messages": [...]} / {"input": {"messages": [...]}};
    3. 直接传入消息数组。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return _prompt_from_messages(value)
    if not isinstance(value, dict):
        return ""

    if "input" in value:
        return extract_prompt(value.get("input"))
    if isinstance(value.get("messages"), list):
        return _prompt_from_messages(value["messages"])
    return ""


def _prompt_from_messages(messages: list[Any]) -> str:
    """从消息数组尾部往前查找最后一条非空用户输入。"""
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
    "ThreadStatus",
    "content_text",
    "extract_prompt",
    "message_json",
    "messages_json",
    "now_iso",
]
