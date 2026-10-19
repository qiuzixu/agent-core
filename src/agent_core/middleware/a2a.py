"""A2A（Agent-to-Agent）通信：子 Agent 注册 + HTTP 派发。

工具名前缀 a2a_ 的调用自动路由到对应子 Agent 的 HTTP 端点。
子 Agent 通过 AgentCard 描述（name / url / skills），由业务层注册。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

A2A_TOOL_PREFIX = "a2a_"


@dataclass
class AgentCard:
    """子 Agent 描述卡（发现 + 路由）。"""

    name: str
    url: str
    description: str = ""
    skills: list[str] = field(default_factory=list)
    timeout_seconds: float = 30.0


@dataclass
class A2AMessage:
    """A2A 消息体（简化版 Google A2A 协议）。"""

    role: str = "user"
    parts: list[dict[str, Any]] = field(default_factory=list)
    context_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role, "parts": self.parts,
            "contextId": self.context_id,
            "taskId": self.task_id or uuid.uuid4().hex[:16],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "A2AMessage":
        return cls(
            role=data.get("role", "agent"),
            parts=data.get("parts", []),
            context_id=data.get("contextId", ""),
            task_id=data.get("taskId", ""),
        )

    @property
    def text(self) -> str:
        """取第一个 text part 的内容。"""
        for part in self.parts:
            if part.get("type") == "text":
                return str(part.get("text", ""))
        return ""


def _http_post_json(url: str, body: bytes, timeout: float) -> dict[str, Any]:
    """同步 HTTP POST JSON（在线程池中运行）。"""
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class A2AAgentRegistry:
    """子 Agent 注册表 + HTTP 派发。"""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._agents: dict[str, AgentCard] = {}
        self._default_timeout = timeout_seconds

    def register(self, card: AgentCard) -> None:
        self._agents[card.name] = card
        logger.info("A2A 注册子 Agent: %s -> %s", card.name, card.url)

    def unregister(self, name: str) -> None:
        self._agents.pop(name, None)

    @property
    def agent_names(self) -> list[str]:
        return list(self._agents)

    def get_card(self, name: str) -> AgentCard | None:
        return self._agents.get(name)

    def tool_definitions(self) -> list[dict[str, Any]]:
        """把每个子 Agent 包装为虚拟工具定义。"""
        tools: list[dict[str, Any]] = []
        for card in self._agents.values():
            tools.append({
                "name": f"{A2A_TOOL_PREFIX}{card.name}",
                "description": f"[A2A] {card.description}" if card.description else f"调用子 Agent {card.name}",
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string", "description": "发给子 Agent 的消息"}},
                    "required": ["message"],
                },
            })
        return tools

    async def dispatch(self, agent_name: str, message: A2AMessage) -> A2AMessage:
        """异步派发到子 Agent HTTP 端点。"""
        card = self._agents.get(agent_name)
        if card is None:
            raise KeyError(f"子 Agent 未注册: {agent_name}")
        body = json.dumps(message.to_dict(), ensure_ascii=False).encode("utf-8")
        loop = asyncio.get_running_loop()
        timeout = card.timeout_seconds or self._default_timeout
        try:
            raw = await loop.run_in_executor(None, _http_post_json, card.url, body, timeout)
            response = A2AMessage.from_dict(raw)
            logger.info("A2A %s 响应: %d chars", agent_name, len(response.text))
            return response
        except (URLError, TimeoutError, ValueError, OSError) as exc:
            logger.exception("A2A 派发到 %s 失败", agent_name)
            return A2AMessage(role="agent", parts=[{"type": "text", "text": f"A2A 调用失败: {exc}"}],
                              context_id=message.context_id)


class A2AMiddleware(Middleware):
    """before_tool 钩子：把 a2a_<name> 工具调用路由到子 Agent。"""

    def __init__(self, registry: A2AAgentRegistry | None = None) -> None:
        self._registry = registry or A2AAgentRegistry()

    @property
    def registry(self) -> A2AAgentRegistry:
        return self._registry

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        tool_name = context.tool_name or ""
        if not tool_name.startswith(A2A_TOOL_PREFIX):
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        agent_name = tool_name[len(A2A_TOOL_PREFIX):]
        text = str((context.tool_args or {}).get("message", ""))
        message = A2AMessage(role="user", parts=[{"type": "text", "text": text}])
        try:
            response = await self._registry.dispatch(agent_name, message)
            return MiddlewareResult(
                action=MiddlewareAction.CONTINUE,
                data={"a2a_response": response.text, "a2a_agent": agent_name},
            )
        except Exception as exc:
            logger.exception("A2A %s 调用异常", agent_name)
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error=f"A2A 子 Agent {agent_name} 调用失败: {exc}",
            )


__all__ = ["A2A_TOOL_PREFIX", "A2AMessage", "A2AAgentRegistry", "A2AMiddleware", "AgentCard"]
