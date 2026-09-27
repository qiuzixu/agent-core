"""手写消息类型定义。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Message:
    """基础消息类型。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为 Core 内部的提供商无关格式。"""
        result: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.name:
            result["name"] = self.name
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            # Core 内部保留扁平结构，具体模型适配器负责转换格式。
            result["tool_calls"] = [dict(call) for call in self.tool_calls]
        return result

    def to_openai_dict(self) -> dict[str, Any]:
        """转换为 OpenAI API 格式，保留给现有适配器和 Web API 使用。"""
        result = self.to_dict()
        if self.tool_calls:
            result["tool_calls"] = [
                {
                    "id": call.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": json.dumps(call.get("args", {}), ensure_ascii=False),
                    },
                }
                for call in self.tool_calls
            ]
        return result


def system_message(content: str) -> Message:
    """创建系统消息。"""
    return Message(role="system", content=content)


def user_message(content: str) -> Message:
    """创建用户消息。"""
    return Message(role="user", content=content)


def assistant_message(
    content: str,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
) -> Message:
    """创建助手消息。"""
    return Message(
        role="assistant",
        content=content,
        tool_calls=tool_calls or [],
    )


def tool_message(
    content: str,
    *,
    tool_call_id: str,
    name: str,
) -> Message:
    """创建工具消息。"""
    return Message(
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
        name=name,
    )
