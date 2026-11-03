"""Plan-Execute、Supervisor 和 Reflexion 模式的基础行为测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_core import (
    AgentMode,
    ExecutionPlan,
    PlanExecuteAgent,
    PlanExecutionResult,
    PlanStepResult,
    SupervisorAgent,
    ReflexionAgent,
    Worker,
    SwarmAgent,
    SwarmWorker,
    answer_text,
)
from agent_core.protocol.messages import Message, assistant_message


class ScriptedModel:
    """按脚本返回模型消息的最小测试适配器。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("脚本模型没有更多返回值")
        return assistant_message(self._responses.pop(0))

    def stream_chat(self, messages: list[Message], **kwargs: Any):
        raise AssertionError("高级模式测试不应使用流式接口")


class ScriptedExecutor:
    """记录输入并按脚本返回输出的最小执行器。"""

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = list(outputs)
        self.inputs: list[str] = []

    async def run(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> str:
        self.inputs.append(user_input)
        if not self._outputs:
            raise AssertionError("脚本执行器没有更多返回值")
        return self._outputs.pop(0)


@pytest.mark.asyncio
async def test_plan_execute_runs_steps_then_synthesizes() -> None:
    plan = {
        "objective": "完成研究并总结",
        "strategy": "先查资料，再总结",
        "steps": [
            {"id": "s1", "title": "查资料", "instruction": "收集要点"},
            {"id": "s2", "title": "总结", "instruction": "输出结论"},
        ],
    }
    model = ScriptedModel([json.dumps(plan, ensure_ascii=False), "这是最终答案"])
    executor = ScriptedExecutor(["资料结果", "总结结果"])
    agent = PlanExecuteAgent(model, executor, max_steps=2)

    result = await agent.run("完成研究并总结", thread_id="thread-1")

    assert AgentMode.PLAN_EXECUTE.value == "plan_execute"
    assert result.answer == "这是最终答案"
    assert [item.title for item in result.plan.steps] == ["查资料", "总结"]
    assert [item.output for item in result.steps] == ["资料结果", "总结结果"]
    assert "收集要点" in executor.inputs[0]
    assert "资料结果" in executor.inputs[1]


@pytest.mark.asyncio
async def test_supervisor_routes_worker_then_finalizes() -> None:
    route_1 = {"target": "researcher", "instruction": "查找资料", "reason": "需要事实"}
    route_2 = {"target": "FINAL", "instruction": "", "reason": "信息足够"}
    model = ScriptedModel(
        [
            json.dumps(route_1, ensure_ascii=False),
            json.dumps(route_2, ensure_ascii=False),
            "Multi-Agent 最终答案",
        ]
    )
    worker = ScriptedExecutor(["Worker 结果"])
    agent = SupervisorAgent(
        model,
        [Worker(name="researcher", description="负责检索", agent=worker)],
        max_rounds=2,
    )

    result = await agent.run("完成研究", thread_id="thread-2")

    assert result.answer == "Multi-Agent 最终答案"
    assert [route.target for route in result.routes] == ["researcher"]
    assert result.routes[0].output == "Worker 结果"
    assert worker.inputs == ["查找资料"]


@pytest.mark.asyncio
async def test_reflexion_retries_with_feedback() -> None:
    model = ScriptedModel([])
    critic = ScriptedModel(
        [
            json.dumps(
                {
                    "acceptable": False,
                    "score": 3,
                    "feedback": "缺少结论",
                    "revised_instruction": "补充结论",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "acceptable": True,
                    "score": 8,
                    "feedback": "修订后完整",
                },
                ensure_ascii=False,
            ),
        ]
    )
    executor = ScriptedExecutor(["第一版答案", "修订后的完整答案"])
    agent = ReflexionAgent(executor, critic, max_reflections=1, score_threshold=7)

    result = await agent.run("回答问题", thread_id="thread-3")

    assert result.answer == "修订后的完整答案"
    assert result.attempts == 2
    assert result.reflections[0].feedback == "缺少结论"
    assert "第一版答案" in executor.inputs[1]

@pytest.mark.asyncio
async def test_swarm_handoffs_between_members_then_final() -> None:
    # 退款成员的输出完成后，模型决定移交给发票成员
    decision = {"handoff_to": "invoice", "reason": "发票问题需要技术支持"}
    model = ScriptedModel([json.dumps(decision, ensure_ascii=False)])
    refund = ScriptedExecutor(["退款已成功"])
    invoice = ScriptedExecutor(["发票已开具"])
    agent = SwarmAgent(
        model,
        [
            SwarmWorker(
                name="refund",
                description="退款专员",
                agent=refund,
                handoffs=("invoice",),
            ),
            SwarmWorker(
                name="invoice",
                description="发票专员",
                agent=invoice,
            ),
        ],
        entry="refund",
        max_hops=2,
    )

    result = await agent.run("退款并开发票", thread_id="thread-4")

    assert AgentMode.SWARM.value == "swarm"
    assert result.answer == "发票已开具"
    assert len(result.handoffs) == 1
    assert result.handoffs[0].from_agent == "refund"
    assert result.handoffs[0].to_agent == "invoice"
    assert "退款已成功" in invoice.inputs[0]


@pytest.mark.asyncio
async def test_swarm_terminal_member_returns_directly() -> None:
    model = ScriptedModel([])
    worker = ScriptedExecutor(["直接回答"])
    agent = SwarmAgent(
        model,
        [SwarmWorker(name="solo", description="单成员", agent=worker)],
    )

    result = await agent.run("你好")

    assert result.answer == "直接回答"
    assert result.handoffs == []


@pytest.mark.asyncio
async def test_swarm_rejects_handoff_outside_whitelist() -> None:
    decision = {"handoff_to": "hacker", "reason": "越权移交"}
    model = ScriptedModel([json.dumps(decision, ensure_ascii=False)])
    member = ScriptedExecutor(["输出"])
    agent = SwarmAgent(
        model,
        [
            SwarmWorker(name="a", description="A", agent=member, handoffs=("b",)),
            SwarmWorker(name="b", description="B", agent=member),
        ],
        entry="a",
    )

    with pytest.raises(ValueError, match="白名单外"):
        await agent.run("输入")


@pytest.mark.asyncio
async def test_nested_result_is_normalized_for_supervisor() -> None:
    """高级模式嵌套时，Worker 结果应统一成可序列化文本。"""
    route_1 = {"target": "nested", "instruction": "执行子任务", "reason": "需要子 Agent"}
    route_2 = {"target": "FINAL", "instruction": "", "reason": "信息足够"}
    model = ScriptedModel(
        [json.dumps(route_1, ensure_ascii=False), json.dumps(route_2, ensure_ascii=False), "最终答案"]
    )
    nested = PlanExecutionResult(
        answer="嵌套答案",
        plan=ExecutionPlan(objective="子任务", strategy="直接执行", steps=[]),
        steps=[
            PlanStepResult(
                step_id="s1",
                title="子步骤",
                instruction="执行",
                output="完成",
            )
        ],
    )

    class NestedExecutor:
        async def run(self, user_input: str, **kwargs: Any) -> PlanExecutionResult:
            return nested

    result = await SupervisorAgent(
        model,
        [Worker(name="nested", description="嵌套 Agent", agent=NestedExecutor())],
        max_rounds=2,
    ).run("完成任务")

    assert result.routes[0].output == "嵌套答案"
    assert result.to_dict()["routes"][0]["output"] == "嵌套答案"
    assert answer_text(nested) == "嵌套答案"


@pytest.mark.asyncio
async def test_plan_execute_honors_dependencies_and_rejects_cycles() -> None:
    """计划执行应按依赖排序，并拒绝循环依赖。"""
    ordered_plan = {
        "objective": "按依赖执行",
        "strategy": "先准备再处理",
        "steps": [
            {"id": "process", "title": "处理", "instruction": "处理数据", "depends_on": ["prepare"]},
            {"id": "prepare", "title": "准备", "instruction": "准备数据"},
        ],
    }
    model = ScriptedModel([json.dumps(ordered_plan, ensure_ascii=False), "完成"])
    executor = ScriptedExecutor(["准备完成", "处理完成"])
    result = await PlanExecuteAgent(model, executor, max_steps=2).run("按依赖执行")

    assert [item.step_id for item in result.steps] == ["prepare", "process"]
    assert executor.inputs[0] == "准备数据"

    cyclic_plan = {
        "objective": "循环",
        "strategy": "无",
        "steps": [
            {"id": "a", "title": "A", "instruction": "A", "depends_on": ["b"]},
            {"id": "b", "title": "B", "instruction": "B", "depends_on": ["a"]},
        ],
    }
    with pytest.raises(ValueError, match="循环依赖"):
        await PlanExecuteAgent(
            ScriptedModel([json.dumps(cyclic_plan, ensure_ascii=False)]),
            ScriptedExecutor([]),
        ).run("循环")


@pytest.mark.asyncio
async def test_plan_execute_passes_memory_to_planner_and_finalizer() -> None:
    """规划和总结模型都应看到会话记忆。"""
    plan = {
        "objective": "记忆测试",
        "strategy": "一步完成",
        "steps": [{"id": "s1", "title": "执行", "instruction": "执行"}],
    }
    model = ScriptedModel([json.dumps(plan, ensure_ascii=False), "完成"])
    await PlanExecuteAgent(model, ScriptedExecutor(["结果"])).run(
        "记忆测试", memory_context="用户偏好简洁回答"
    )

    assert all(
        any("用户偏好简洁回答" in message.content for message in call)
        for call in model.calls
    )


@pytest.mark.asyncio
async def test_swarm_handoff_model_receives_previous_transcript() -> None:
    """后续 Swarm 移交决策应包含此前成员的输出。"""
    model = ScriptedModel(
        [
            json.dumps({"handoff_to": "b", "reason": "需要继续处理"}, ensure_ascii=False),
            json.dumps({"handoff_to": "c", "reason": "继续收尾"}, ensure_ascii=False),
        ]
    )
    first = ScriptedExecutor(["第一步结果"])
    second = ScriptedExecutor(["第二步结果"])
    third = ScriptedExecutor(["最终结果"])
    result = await SwarmAgent(
        model,
        [
            SwarmWorker(name="a", description="A", agent=first, handoffs=("b",)),
            SwarmWorker(name="b", description="B", agent=second, handoffs=("c",)),
            SwarmWorker(name="c", description="C", agent=third),
        ],
        entry="a",
    ).run("多步任务")

    assert result.answer == "最终结果"
    assert "第一步结果" in model.calls[1][-1].content
