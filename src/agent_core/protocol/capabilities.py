"""Agent HTTP 能力协议。

能力声明放在 Core 的协议层，Web API 只负责把它序列化返回。
这样前端可以先探测能力，再决定是否使用流式输出、审批、恢复和事件接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentCapabilities:
    """一个 Agent 服务对外暴露的通用运行能力。"""

    protocol_version: str = "1.0"
    agent_id: str = "agent"
    runtime: str = "agent-core"
    transports: tuple[str, ...] = ("http",)
    features: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "threads",
                "runs",
                "run_events",
                "run_stream",
                "run_cancel",
                "run_resume",
                "checkpoint_recovery",
                "approval",
                "client_actions",
                "context_usage",
            }
        )
    )

    def to_dict(self) -> dict[str, Any]:
        """返回稳定的 JSON 字段，供 HTTP 层直接使用。"""
        return {
            "protocol_version": self.protocol_version,
            "agent_id": self.agent_id,
            "runtime": self.runtime,
            "transports": list(self.transports),
            "features": sorted(self.features),
        }


__all__ = ["AgentCapabilities"]
