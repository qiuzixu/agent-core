"""AgentRuntime 自定义执行器（RunExecutor）的回归测试。"""

from __future__ import annotations

import unittest
from typing import Any

from agent_core import (
    AgentMode,
    AgentRuntime,
    ExecutionPlan,
    PlanExecutionResult,
    ReActAgent,
    RunContext,
    RunExecutor,
    ToolExecutor,
    ToolRegistry,
    assistant_message,
)


class _EchoModel:
    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:
        del messages, kwargs
        return assistant_message("model-answer")

    async def stream_chat(self, messages: list[Any], **kwargs: Any):
        yield assistant_message("model-answer")


class RunExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_executor_receives_context_and_persists(self) -> None:
        """自定义执行器接管 Agent 调用，生命周期仍由 Runtime 收口。"""
        registry = ToolRegistry()
        agent = ReActAgent(
            _EchoModel(),
            ToolExecutor(registry),
            system_prompt="s",
            tool_definitions=[],
        )
        seen: dict[str, Any] = {}

        async def executor(
            context: RunContext,
            user_input: str,
            *,
            memory_context: str | None = None,
            checkpoint_state: dict[str, Any] | None = None,
        ) -> str:
            seen["run_id"] = context.run_id
            seen["status"] = context.status
            seen["input"] = user_input
            return f"custom:{user_input}"

        executor_check: RunExecutor = executor  # 协议形状校验
        runtime = AgentRuntime(agent, run_executor=executor_check)
        context = await runtime.run("t1", "你好")
        self.assertEqual(seen["run_id"], context.run_id)
        self.assertEqual(seen["status"], "running")
        self.assertEqual(seen["input"], "你好")
        self.assertEqual(context.status, "completed")
        # 完整生命周期事件由 Runtime 收口（含 run_started/run_completed）
        event_types = [event.event_type for event in context.events]
        self.assertIn("run_started", event_types)
        self.assertIn("run_completed", event_types)
        # 恢复接口对自定义执行器同样生效（从 checkpoint 续跑或直接完成短路）
        if context.checkpoint:
            resumed = await runtime.resume("t1", context.run_id)
            await runtime.wait("t1", context.run_id)
            self.assertIn(resumed.status, {"completed", "failed", "cancelled", "interrupted"})

    async def test_custom_executor_normalizes_advanced_result(self) -> None:
        """Runtime 应把高级模式结果对象归一化为文本终态。"""
        registry = ToolRegistry()
        agent = ReActAgent(
            _EchoModel(),
            ToolExecutor(registry),
            system_prompt="s",
            tool_definitions=[],
        )

        async def executor(
            context: RunContext,
            user_input: str,
            *,
            memory_context: str | None = None,
            checkpoint_state: dict[str, Any] | None = None,
        ) -> PlanExecutionResult:
            del context, user_input, memory_context, checkpoint_state
            return PlanExecutionResult(
                answer="高级模式答案",
                plan=ExecutionPlan(objective="测试", strategy="直接", steps=[]),
                steps=[],
            )

        runtime = AgentRuntime(agent, run_executor=executor)
        context = await runtime.run("t1", "你好")

        completed = [event for event in context.events if event.event_type == "run_completed"]
        self.assertEqual(completed[-1].payload["answer"], "高级模式答案")

    async def test_runtime_accepts_advanced_mode_directly(self) -> None:
        """AgentRuntime 可以直接托管高级模式的非流式执行。"""

        class AdvancedAgent:
            mode = AgentMode.PLAN_EXECUTE

            async def run(self, user_input: str, **kwargs: Any) -> PlanExecutionResult:
                del user_input, kwargs
                return PlanExecutionResult(
                    answer="直接托管答案",
                    plan=ExecutionPlan(objective="测试", strategy="直接", steps=[]),
                    steps=[],
                )

        runtime = AgentRuntime(AdvancedAgent())  # type: ignore[arg-type]
        context = await runtime.run("t1", "你好")

        self.assertEqual(context.status, "completed")
        self.assertEqual(context.metadata["agent_mode"], "plan_execute")


if __name__ == "__main__":
    unittest.main()
