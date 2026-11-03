"""Reflexion / Self-Critic Agent。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_core.model import ModelAdapter, chat_structured, StructuredOutputSpec
from agent_core.protocol.messages import system_message, user_message
from agent_core.runtime.modes import AgentMode, SupportsRun, answer_text


@dataclass(frozen=True)
class Reflection:
    """Self-Critic / Reflexion 的一次评审结果。"""

    acceptable: bool
    score: int
    feedback: str
    revised_instruction: str = ""


@dataclass(frozen=True)
class ReflexionResult:
    """带反思重试的执行结果。"""

    answer: str
    reflections: list[Reflection]
    attempts: int

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""
        return {
            "answer": self.answer,
            "attempts": self.attempts,
            "reflections": [
                {
                    "acceptable": item.acceptable,
                    "score": item.score,
                    "feedback": item.feedback,
                    "revised_instruction": item.revised_instruction,
                }
                for item in self.reflections
            ],
        }


_REFLECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["acceptable", "score", "feedback"],
    "properties": {
        "acceptable": {"type": "boolean"},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "feedback": {"type": "string"},
        "revised_instruction": {"type": "string"},
    },
}


def _reflection_decoder(value: dict[str, Any]) -> Reflection:
    """解析 Self-Critic JSON。"""
    return Reflection(
        acceptable=bool(value.get("acceptable", False)),
        score=int(value.get("score", 0)),
        feedback=str(value.get("feedback", "")).strip(),
        revised_instruction=str(value.get("revised_instruction", "")).strip(),
    )


class ReflexionAgent:
    """执行、评审、反思并按需重试的 Agent。"""

    mode = AgentMode.REFLEXION

    def __init__(
        self,
        agent: SupportsRun,
        critic_model: ModelAdapter,
        *,
        max_reflections: int = 2,
        score_threshold: int = 7,
    ) -> None:
        """创建 Reflexion / Self-Critic Agent。"""
        if not 0 <= int(score_threshold) <= 10:
            raise ValueError("score_threshold 必须在 0 到 10 之间")
        self._agent = agent
        self._critic_model = critic_model
        self._max_reflections = max(0, int(max_reflections))
        self._score_threshold = int(score_threshold)

    async def run(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> ReflexionResult:
        """执行一次，评审不合格时带着反馈继续修订。"""
        initial_metadata = {
            **(run_metadata or {}),
            "agent_mode": AgentMode.REFLEXION.value,
            "reflection_attempt": 1,
        }
        answer = answer_text(
            await self._agent.run(
                user_input,
                memory_context=memory_context,
                thread_id=thread_id,
                run_metadata=initial_metadata,
            )
        )
        reflections: list[Reflection] = []
        attempts = 1
        if self._max_reflections == 0:
            return ReflexionResult(answer=answer, reflections=reflections, attempts=attempts)

        memory_hint = f"\n\n相关会话记忆：\n{memory_context}" if memory_context else ""
        for review_round in range(self._max_reflections + 1):
            review_messages = [
                system_message(
                    "你是严格但务实的 Self-Critic。检查答案是否完整、准确、可执行。"
                    "只输出 JSON。不要因为风格偏好而拒绝答案。"
                ),
                user_message(
                    f"原始请求：{user_input}\n\n候选答案：\n{answer}\n\n"
                    f"score 使用 0-10，{self._score_threshold} 分及以上视为合格。"
                    f"{memory_hint}"
                ),
            ]
            spec = StructuredOutputSpec(
                name="self_critique",
                schema=_REFLECTION_SCHEMA,
                decoder=_reflection_decoder,
            )
            reflection = (await chat_structured(self._critic_model, review_messages, spec)).value
            reflections.append(reflection)

            if reflection.acceptable or reflection.score >= self._score_threshold:
                return ReflexionResult(answer=answer, reflections=reflections, attempts=attempts)

            if review_round >= self._max_reflections:
                # 已达到修订预算，返回最后一版并保留不合格评审记录，避免静默丢失质量信号。
                return ReflexionResult(answer=answer, reflections=reflections, attempts=attempts)

            attempts += 1
            revision_input = (
                f"{user_input}\n\n请修订上一版答案。\n\n上一版答案：\n{answer}\n\n"
                f"评审反馈：{reflection.feedback}"
            )
            if reflection.revised_instruction:
                revision_input += f"\n\n修订要求：{reflection.revised_instruction}"

            revision_metadata = {
                **(run_metadata or {}),
                "agent_mode": AgentMode.REFLEXION.value,
                "reflection_attempt": attempts,
            }
            answer = answer_text(
                await self._agent.run(
                    revision_input,
                    memory_context=memory_context,
                    thread_id=thread_id,
                    run_metadata=revision_metadata,
                )
            )

        return ReflexionResult(answer=answer, reflections=reflections, attempts=attempts)



__all__ = [
    "Reflection",
    "ReflexionAgent",
    "ReflexionResult",
]
