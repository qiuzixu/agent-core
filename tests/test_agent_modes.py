"""Plan-Execute、Supervisor 和 Reflexion 模式的基础行为测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_core import (
    AgentMode,
    PlanExecuteAgent,
    SupervisorAgent,
    ReflexionAgent,
    Worker,
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
            )
        ]
    )
    executor = ScriptedExecutor(["第一版答案", "修订后的完整答案"])
    agent = ReflexionAgent(executor, critic, max_reflections=1, score_threshold=7)

    result = await agent.run("回答问题", thread_id="thread-3")

    assert result.answer == "修订后的完整答案"
    assert result.attempts == 2
    assert result.reflections[0].feedback == "缺少结论"
    assert "第一版答案" in executor.inputs[1]
