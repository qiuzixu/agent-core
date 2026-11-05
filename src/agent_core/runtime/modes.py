"""Agent 执行模式与最小执行器协议。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, TypeAlias, runtime_checkable


class AgentMode(StrEnum):
    """内置 Agent 执行模式。"""

    REACT = "react"
    PLAN_EXECUTE = "plan_execute"
    SUPERVISOR = "supervisor"
    REFLEXION = "reflexion"
    SWARM = "swarm"


@runtime_checkable
class SupportsAnswer(Protocol):
    """可被编排器提取最终文本的结果对象。"""

    answer: str


AgentOutput: TypeAlias = str | SupportsAnswer


def answer_text(value: AgentOutput) -> str:
    """把字符串或高级模式结果统一转换为最终答案文本。"""
    if isinstance(value, str):
        return value
    answer = getattr(value, "answer", None)
    if isinstance(answer, str):
        return answer
    raise TypeError("Agent 执行结果必须是 str 或包含 answer 字段的结果对象")


def bounded_text(value: str, *, max_chars: int = 12000) -> str:
    """限制编排上下文长度，优先保留最新结果。"""
    limit = max(256, int(max_chars))
    if len(value) <= limit:
        return value
    return "[前序结果已压缩]\n" + value[-limit:]


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
    ) -> AgentOutput:
        """执行一次子任务并返回文本结果。"""
        ...


__all__ = [
    "AgentOutput",
    "AgentMode",
    "SupportsRun",
    "SupportsAnswer",
    "answer_text",
    "bounded_text",
]
