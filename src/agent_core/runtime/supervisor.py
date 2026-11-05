"""Supervisor 多 Agent 执行器。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from agent_core.model import ModelAdapter, chat_structured, StructuredOutputSpec
from agent_core.protocol.messages import system_message, user_message
from agent_core.runtime.modes import AgentMode, SupportsRun, answer_text, bounded_text


@dataclass(frozen=True)
class Worker:
    """Supervisor 模式中的一个可路由 Worker。"""

    name: str
    description: str
    agent: SupportsRun


@dataclass(frozen=True)
class SupervisorRoute:
    """一次 Supervisor 路由决策。"""

    target: str
    instruction: str
    reason: str
    output: str = ""
    status: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""
        return {
            "target": self.target,
            "instruction": self.instruction,
            "reason": self.reason,
            "output": self.output,
            "status": self.status,
        }


@dataclass(frozen=True)
class SupervisorResult:
    """Supervisor 多 Agent 执行结果。"""

    answer: str
    routes: list[SupervisorRoute]

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""
        return {
            "answer": self.answer,
            "routes": [route.to_dict() for route in self.routes],
        }


_SUPERVISOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["target", "instruction", "reason"],
    "properties": {
        "target": {"type": "string"},
        "instruction": {"type": "string"},
        "reason": {"type": "string"},
    },
}


def _supervisor_decoder(value: dict[str, Any]) -> SupervisorRoute:
    """解析 Supervisor 路由 JSON。"""
    target = str(value.get("target", "")).strip()
    if target.casefold() == "final":
        target = "FINAL"
    return SupervisorRoute(
        target=target,
        instruction=str(value.get("instruction", "")).strip(),
        reason=str(value.get("reason", "")).strip(),
    )


class SupervisorAgent:
    """监督者路由多个 Worker 的 Multi-Agent 执行器。"""

    mode = AgentMode.SUPERVISOR

    def __init__(
        self,
        llm: ModelAdapter,
        workers: list[Worker],
        *,
        max_rounds: int = 4,
        max_transcript_chars: int = 12000,
    ) -> None:
        """创建 Supervisor Agent。"""
        if not workers:
            raise ValueError("Supervisor 至少需要一个 Worker")
        names = [worker.name for worker in workers]
        if len(set(names)) != len(names):
            raise ValueError("Worker 名称不能重复")
        self._llm = llm
        self._workers = {worker.name: worker for worker in workers}
        self._max_rounds = max(1, int(max_rounds))
        self._max_transcript_chars = max(256, int(max_transcript_chars))

    async def run(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> SupervisorResult:
        """路由 Worker、收集结果，并在足够信息后汇总最终答案。"""
        worker_text = "\n".join(
            f"- {worker.name}: {worker.description}" for worker in self._workers.values()
        )
        transcript = ""
        routes: list[SupervisorRoute] = []
        seen_routes: set[tuple[str, str]] = set()
        memory_hint = f"\n\n相关会话记忆：\n{memory_context}" if memory_context else ""

        for round_number in range(1, self._max_rounds + 1):
            route_messages = [
                system_message(
                    "你是 Multi-Agent 监督者。根据目标选择一个 Worker，或返回 FINAL 表示信息已足够。"
                    "只输出 JSON。target 必须是可用 Worker 名称或 FINAL。"
                ),
                user_message(
                    f"用户目标：{user_input}\n\n可用 Worker：\n{worker_text}\n\n"
                    f"已收集信息：\n{bounded_text(transcript, max_chars=self._max_transcript_chars) or '<empty>'}{memory_hint}"
                ),
            ]
            spec = StructuredOutputSpec(
                name="supervisor_route",
                schema=_SUPERVISOR_SCHEMA,
                decoder=_supervisor_decoder,
            )
            route = (await chat_structured(self._llm, route_messages, spec)).value

            if route.target == "FINAL":
                final_messages = [
                    system_message("你是 Multi-Agent 总控。请基于 Worker 结果合成最终答案。"),
                    user_message(
                        f"用户目标：{user_input}\n\n"
                        f"{memory_hint}已收集信息：\n{bounded_text(transcript, max_chars=self._max_transcript_chars) or '<empty>'}"
                    ),
                ]
                final = await self._llm.chat(final_messages)
                return SupervisorResult(answer=final.content, routes=routes)

            worker = self._workers.get(route.target)
            if worker is None:
                routes.append(replace(route, status="failed"))
                raise ValueError(f"Supervisor 选择了未知 Worker：{route.target}")
            if not route.instruction:
                routes.append(replace(route, status="failed"))
                raise ValueError(f"Supervisor 为 Worker {route.target} 生成了空指令")
            route_key = (route.target, route.instruction)
            if route_key in seen_routes:
                routes.append(replace(route, status="failed"))
                raise RuntimeError(f"Supervisor 重复路由相同任务：{route.target}")
            seen_routes.add(route_key)

            step_metadata: dict[str, Any] = {
                **(run_metadata or {}),
                "agent_mode": AgentMode.SUPERVISOR.value,
                "supervisor_round": round_number,
                "supervisor_target": route.target,
            }
            try:
                output = answer_text(
                    await worker.agent.run(
                        route.instruction,
                        memory_context=memory_context,
                        thread_id=thread_id,
                        run_metadata=step_metadata,
                    )
                )
                executed = replace(route, output=output)
                routes.append(executed)
                transcript = bounded_text(
                    f"{transcript}\n[{worker.name}] {output}",
                    max_chars=self._max_transcript_chars,
                )
            except Exception as exc:
                routes.append(replace(route, status="failed", output=str(exc)))
                raise

        raise RuntimeError("Supervisor 达到最大轮次仍没有返回最终答案")




__all__ = [
    "SupervisorAgent",
    "SupervisorResult",
    "SupervisorRoute",
    "Worker",
]
