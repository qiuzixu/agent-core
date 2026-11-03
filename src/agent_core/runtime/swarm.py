"""Swarm 去中心化多 Agent 协作模式。

与 Supervisor（中心化路由）不同，Swarm 没有总控：控制权在成员
Agent 之间直接移交（handoff）。每个成员只声明自己可以移交给哪些
邻居（白名单），移交链全程记录，便于审计和防死循环。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_core.model import ModelAdapter, chat_structured, StructuredOutputSpec
from agent_core.protocol.messages import system_message, user_message
from agent_core.runtime.modes import AgentMode, SupportsRun, answer_text


@dataclass(frozen=True)
class SwarmWorker:
    """Swarm 中的一个成员 Agent。

    handoffs 为空表示该成员是终点，其输出直接作为最终答案。
    """

    name: str
    description: str
    agent: SupportsRun
    handoffs: tuple[str, ...] = ()


@dataclass(frozen=True)
class HandoffDecision:
    """移交决策：handoff_to 为空表示当前输出即为最终答案。"""

    handoff_to: str
    reason: str


@dataclass(frozen=True)
class HandoffRecord:
    """一次移交的审计记录。"""

    from_agent: str
    to_agent: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SwarmResult:
    """Swarm 模式的最终结果。"""

    answer: str
    handoffs: list[HandoffRecord]

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""
        return {
            "answer": self.answer,
            "handoffs": [item.to_dict() for item in self.handoffs],
        }


_SWARM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["handoff_to", "reason"],
    "properties": {
        "handoff_to": {"type": "string"},
        "reason": {"type": "string"},
    },
}


def _swarm_decoder(value: dict[str, Any]) -> HandoffDecision:
    """解析移交决策 JSON。"""
    return HandoffDecision(
        handoff_to=str(value.get("handoff_to", "")).strip(),
        reason=str(value.get("reason", "")).strip(),
    )


class SwarmAgent:
    """成员之间直接移交控制权的去中心化多 Agent 执行器。

    每一轮先执行当前成员，再用独立模型调用判断是否移交给该成员
    白名单中的邻居；移交目标不在白名单时直接报错，避免失控。
    """

    mode = AgentMode.SWARM

    def __init__(
        self,
        llm: ModelAdapter,
        workers: list[SwarmWorker],
        *,
        entry: str | None = None,
        max_hops: int = 4,
    ) -> None:
        """创建 Swarm 执行器并校验移交白名单的完整性。"""
        if not workers:
            raise ValueError("Swarm 至少需要一个成员")
        names = [worker.name for worker in workers]
        if len(set(names)) != len(names):
            raise ValueError("Swarm 成员名称不能重复")
        name_set = set(names)
        for worker in workers:
            invalid = [target for target in worker.handoffs if target not in name_set]
            if invalid:
                raise ValueError(
                    f"成员 {worker.name} 的移交目标不存在：{invalid}"
                )
        if entry is not None and entry not in name_set:
            raise ValueError(f"Swarm 入口成员不存在：{entry}")
        self._llm = llm
        self._workers = {worker.name: worker for worker in workers}
        self._entry = entry or names[0]
        self._max_hops = max(0, int(max_hops))

    async def run(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> SwarmResult:
        """从入口成员开始执行，沿移交白名单在成员间传递控制权。"""
        current = self._workers[self._entry]
        handoffs: list[HandoffRecord] = []
        seen_edges: set[tuple[str, str]] = set()
        transcript = ""
        member_input = user_input

        for hop in range(self._max_hops + 1):
            member_metadata: dict[str, Any] = {
                **(run_metadata or {}),
                "agent_mode": AgentMode.SWARM.value,
                "swarm_hop": hop,
                "swarm_current": current.name,
            }
            output = answer_text(
                await current.agent.run(
                    member_input,
                    memory_context=memory_context,
                    thread_id=thread_id,
                    run_metadata=member_metadata,
                )
            )

            # 终点成员（无移交目标）直接返回答案
            if not current.handoffs:
                return SwarmResult(answer=output, handoffs=handoffs)

            memory_hint = f"\n\n相关会话记忆：\n{memory_context}" if memory_context else ""
            decision_messages = [
                system_message(
                    "你是移交协调器。判断当前成员的输出是否已能回答用户。"
                    "如果已能回答，handoff_to 返回空字符串；否则只能从该成员"
                    "声明的移交给手中选择一个。只输出 JSON。"
                ),
                user_message(
                    f"用户输入：{user_input}\n\n"
                    f"当前成员：{current.name}\n"
                    f"移交给手：{', '.join(current.handoffs)}\n\n"
                    f"前序成员记录：\n{transcript or '<empty>'}\n\n"
                    f"当前成员输出：\n{output}"
                    f"{memory_hint}"
                ),
            ]
            spec = StructuredOutputSpec(
                name="swarm_handoff",
                schema=_SWARM_SCHEMA,
                decoder=_swarm_decoder,
            )
            decision = (await chat_structured(self._llm, decision_messages, spec)).value

            if not decision.handoff_to:
                return SwarmResult(answer=output, handoffs=handoffs)

            if decision.handoff_to not in current.handoffs:
                raise ValueError(
                    f"成员 {current.name} 试图移交给白名单外的目标：{decision.handoff_to}"
                )
            edge = (current.name, decision.handoff_to)
            if edge in seen_edges:
                raise RuntimeError(f"Swarm 检测到重复移交边：{current.name} -> {decision.handoff_to}")
            seen_edges.add(edge)

            handoffs.append(
                HandoffRecord(
                    from_agent=current.name,
                    to_agent=decision.handoff_to,
                    reason=decision.reason,
                )
            )
            transcript += f"\n[{current.name}] {output}"
            current = self._workers[decision.handoff_to]
            member_input = (
                f"{user_input}\n\n前序成员的执行记录：\n{transcript}"
            )

        raise RuntimeError("Swarm 达到最大移交次数仍没有返回最终答案")


__all__ = [
    "HandoffDecision",
    "HandoffRecord",
    "SwarmAgent",
    "SwarmResult",
    "SwarmWorker",
]
