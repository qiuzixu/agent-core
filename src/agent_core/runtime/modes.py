"""Agent 执行模式与最小执行器协议。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class AgentMode(StrEnum):
    """内置 Agent 执行模式。"""

    REACT = "react"
    PLAN_EXECUTE = "plan_execute"
    SUPERVISOR = "supervisor"
    REFLEXION = "reflexion"
    SWARM = "swarm"


@runtime_checkable
class SupportsRun(Protocol):
    """所有高级模式使用的最小执行器协议。"""

    async def run(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> str:
        """执行一次子任务并返回文本结果。"""
        ...


__all__ = [
    "AgentMode",
    "SupportsRun",
]
