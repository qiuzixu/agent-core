"""Plan-and-Execute Agent。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_core.model import ModelAdapter, chat_structured, StructuredOutputSpec
from agent_core.protocol.messages import system_message, user_message
from agent_core.runtime.modes import AgentMode, SupportsRun, answer_text, bounded_text


@dataclass(frozen=True)
class PlanStep:
    """Plan-and-Execute 里的一个执行步骤。"""

    id: str
    title: str
    instruction: str
    depends_on: list[str] = field(default_factory=list)
    acceptance: str = ""
    agent: str = ""


@dataclass(frozen=True)
class ExecutionPlan:
    """规划模型生成的完整执行计划。"""

    objective: str
    strategy: str
    steps: list[PlanStep]

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""
        return {
            "objective": self.objective,
            "strategy": self.strategy,
            "steps": [
                {
                    "id": step.id,
                    "title": step.title,
                    "instruction": step.instruction,
                    "depends_on": list(step.depends_on),
                    "acceptance": step.acceptance,
                    "agent": step.agent,
                }
                for step in self.steps
            ],
        }


@dataclass(frozen=True)
class PlanStepResult:
    """一个计划步骤的执行结果。"""

    step_id: str
    title: str
    instruction: str
    output: str
    status: str = "completed"
    error: str = ""


@dataclass(frozen=True)
class PlanExecutionResult:
    """Plan-and-Execute 的最终结果。"""

    answer: str
    plan: ExecutionPlan
    steps: list[PlanStepResult]

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化结构。"""
        return {
            "answer": self.answer,
            "plan": self.plan.to_dict(),
            "steps": [
                {
                    "step_id": item.step_id,
                    "title": item.title,
                    "instruction": item.instruction,
                    "output": item.output,
                    "status": item.status,
                    "error": item.error,
                }
                for item in self.steps
            ],
        }


_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["objective", "strategy", "steps"],
    "properties": {
        "objective": {"type": "string"},
        "strategy": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "instruction"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "instruction": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "acceptance": {"type": "string"},
                    "agent": {"type": "string"},
                },
            },
        },
    },
}


def _plan_decoder(value: dict[str, Any]) -> ExecutionPlan:
    """把规划 JSON 转成强类型计划。"""
    steps = [
        PlanStep(
            id=str(item.get("id", f"step-{index + 1}")).strip(),
            title=str(item.get("title", f"步骤 {index + 1}")).strip(),
            instruction=str(item.get("instruction", "")).strip(),
            depends_on=[str(item_dep).strip() for item_dep in item.get("depends_on", [])],
            acceptance=str(item.get("acceptance", "")).strip(),
            agent=str(item.get("agent", "")).strip(),
        )
        for index, item in enumerate(value.get("steps", []))
        if isinstance(item, dict)
    ]
    if not steps:
        raise ValueError("规划输出至少需要一个步骤")
    return ExecutionPlan(
        objective=str(value.get("objective", "")),
        strategy=str(value.get("strategy", "")),
        steps=steps,
    )


class PlanExecuteAgent:
    """先规划、后执行的 Agent。"""

    mode = AgentMode.PLAN_EXECUTE

    def __init__(
        self,
        llm: ModelAdapter,
        executor: SupportsRun,
        *,
        max_steps: int = 5,
        continue_on_step_error: bool = False,
        executors: Mapping[str, SupportsRun] | None = None,
        max_context_chars: int = 12000,
        max_parallel_steps: int = 1,
    ) -> None:
        """创建 Plan-and-Execute Agent。"""
        self._llm = llm
        self._executor = executor
        self._max_steps = max(1, int(max_steps))
        self._continue_on_step_error = bool(continue_on_step_error)
        self._executors = dict(executors or {})
        self._max_context_chars = max(256, int(max_context_chars))
        self._max_parallel_steps = max(1, int(max_parallel_steps))

    async def run(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> PlanExecutionResult:
        """生成计划、逐步执行，并返回最终合成答案。"""
        memory_hint = f"\n\n相关会话记忆：\n{memory_context}" if memory_context else ""
        messages = [
            system_message(
                "你是严谨的任务规划器。只输出 JSON 计划，不要执行工具。"
                "步骤必须按执行顺序排列，每个步骤 instruction 要能独立交给执行 Agent。"
            ),
            user_message(
                f"用户目标：{user_input}\n\n"
                f"最多规划 {self._max_steps} 个步骤。"
                "每个步骤要具体、可验证，避免把多个独立动作压到一个步骤里。"
                f"{memory_hint}"
            ),
        ]
        spec = StructuredOutputSpec(
            name="execution_plan",
            schema=_PLAN_SCHEMA,
            decoder=_plan_decoder,
        )
        structured = await chat_structured(self._llm, messages, spec)
        structured_plan = structured.value
        plan = ExecutionPlan(
            objective=structured_plan.objective,
            strategy=structured_plan.strategy,
            steps=structured_plan.steps[: self._max_steps],
        )
        self._validate_plan(plan)

        step_results: list[PlanStepResult] = []
        previous_context = ""
        completed_ids: set[str] = set()
        failed_ids: set[str] = set()
        pending_steps = list(plan.steps)

        async def execute_step(step: PlanStep, index: int) -> PlanStepResult:
            instruction = step.instruction
            if previous_context:
                instruction = (
                    f"{instruction}\n\n已完成步骤的结果摘要：\n"
                    f"{bounded_text(previous_context, max_chars=self._max_context_chars)}"
                )
            if step.acceptance:
                instruction = f"{instruction}\n\n完成标准：{step.acceptance}"
            step_metadata: dict[str, Any] = {
                **(run_metadata or {}),
                "agent_mode": AgentMode.PLAN_EXECUTE.value,
                "plan_step": index,
                "plan_step_id": step.id,
                "plan_step_dependencies": list(step.depends_on),
            }
            try:
                selected_executor: SupportsRun | None = self._executor
                if step.agent:
                    selected_executor = self._executors.get(step.agent)
                    if selected_executor is None:
                        raise ValueError(f"步骤 {step.id} 指定了未注册的执行 Agent：{step.agent}")
                if selected_executor is None:
                    raise RuntimeError(f"步骤 {step.id} 没有可用的执行 Agent")
                output = answer_text(
                    await selected_executor.run(
                        instruction,
                        memory_context=memory_context,
                        thread_id=thread_id,
                        run_metadata=step_metadata,
                    )
                )
                return PlanStepResult(
                    step_id=step.id,
                    title=step.title,
                    instruction=step.instruction,
                    output=output,
                )
            except Exception as exc:
                return PlanStepResult(
                    step_id=step.id,
                    title=step.title,
                    instruction=step.instruction,
                    output="",
                    status="failed",
                    error=str(exc),
                )

        while pending_steps:
            blocked_steps = [
                step
                for step in pending_steps
                if any(dependency in failed_ids for dependency in step.depends_on)
            ]
            if blocked_steps:
                for blocked in blocked_steps:
                    pending_steps.remove(blocked)
                    failed_ids.add(blocked.id)
                    step_results.append(
                        PlanStepResult(
                            step_id=blocked.id,
                            title=blocked.title,
                            instruction=blocked.instruction,
                            output="",
                            status="blocked",
                            error="前置步骤失败，未执行该步骤",
                        )
                    )
                if not self._continue_on_step_error:
                    raise RuntimeError(f"步骤 {blocked_steps[0].id} 的前置步骤失败")
                continue

            ready = [
                step for step in pending_steps if set(step.depends_on).issubset(completed_ids)
            ]
            if not ready:
                raise ValueError("执行计划存在无法满足的步骤依赖")
            batch = ready[: self._max_parallel_steps]
            for step in batch:
                pending_steps.remove(step)
            batch_results = await asyncio.gather(
                *(execute_step(step, len(step_results) + offset + 1) for offset, step in enumerate(batch))
            )
            for step, result in zip(batch, batch_results, strict=True):
                step_results.append(result)
                if result.status == "completed":
                    completed_ids.add(step.id)
                    previous_context = bounded_text(
                        f"{previous_context}\n[{step.title}] {result.output}",
                        max_chars=self._max_context_chars,
                    )
                else:
                    failed_ids.add(step.id)
                    if not self._continue_on_step_error:
                        raise RuntimeError(f"步骤 {step.id} 执行失败：{result.error}")

        memory_final_hint = f"相关会话记忆：\n{memory_context}\n\n" if memory_context else ""
        final_messages = [
            system_message("你是任务总控。请基于计划与执行结果输出清晰、可用的最终答案。"),
            user_message(
                f"用户目标：{user_input}\n\n计划策略：{plan.strategy}\n\n"
                f"{memory_final_hint}"
                "执行结果：\n"
                + "\n".join(
                    f"[{item.title}] {item.output or item.error}" for item in step_results
                )
            ),
        ]
        final = await self._llm.chat(final_messages)
        return PlanExecutionResult(
            answer=final.content,
            plan=plan,
            steps=step_results,
        )

    @staticmethod
    def _validate_plan(plan: ExecutionPlan) -> None:
        """校验步骤标识和依赖关系，避免静默跳过或死循环。"""
        if not plan.objective.strip():
            raise ValueError("规划目标不能为空")
        if not plan.steps:
            raise ValueError("规划输出至少需要一个步骤")
        ids = [step.id.strip() for step in plan.steps]
        if any(not step_id for step_id in ids):
            raise ValueError("规划步骤 ID 不能为空")
        if len(set(ids)) != len(ids):
            raise ValueError("规划步骤 ID 不能重复")
        known = set(ids)
        for step in plan.steps:
            if not step.instruction.strip():
                raise ValueError(f"步骤 {step.id} 的 instruction 不能为空")
            unknown = [dependency for dependency in step.depends_on if dependency not in known]
            if unknown:
                raise ValueError(f"步骤 {step.id} 依赖了不存在的步骤：{unknown}")
            if step.id in step.depends_on:
                raise ValueError(f"步骤 {step.id} 不能依赖自身")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError("执行计划存在循环依赖")
            if step_id in visited:
                return
            visiting.add(step_id)
            step = next(item for item in plan.steps if item.id == step_id)
            for dependency in step.depends_on:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)




__all__ = [
    "ExecutionPlan",
    "PlanExecuteAgent",
    "PlanStep",
    "PlanStepResult",
    "PlanExecutionResult",
]
