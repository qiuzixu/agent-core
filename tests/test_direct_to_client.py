"""direct_to_client 工具结果短路测试。

验证声明 direct_to_client=True 的工具结果跳过 LLM 二次生成，
直接作为最终回复返回给调用方。
"""

from __future__ import annotations

import json
import unittest
from collections.abc import AsyncIterator

from agent_core import (
    AgentRuntime,
    MemoryRunStore,
    ReActAgent,
    StreamChunk,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    assistant_message,
)


class _Model:
    """记录 LLM 被调用次数的最小模型。"""

    def __init__(self) -> None:
        self.call_count = 0

    async def chat(self, _messages: object, *, tools: object = None) -> object:
        del tools
        self.call_count += 1
        # 第一轮返回工具调用；第二轮返回文本。
        if self.call_count == 1:
            return assistant_message(
                "",
                tool_calls=[{"id": "call-1", "name": "get_detail", "args": {}}],
            )
        return assistant_message("LLM 二次生成的文本")

    async def stream_chat(
        self,
        _messages: object,
        *,
        tools: object = None,
    ) -> AsyncIterator[StreamChunk]:
        del tools
        self.call_count += 1
        yield StreamChunk(text="不应流式输出")


def _get_final_answer(context: object) -> str:
    """从 RunContext 的 run_completed 事件中提取最终回答。"""

    for event in context.events:  # type: ignore[attr-defined]
        if event.event_type == "run_completed":
            return str(event.payload.get("answer", ""))
    return ""


def _register_direct_tool(registry: ToolRegistry) -> None:
    """注册一个声明 direct_to_client=True 的工具。"""

    async def get_detail(**_kwargs: object) -> ToolResult:
        return ToolResult(
            tool_name="get_detail",
            success=True,
            value={"answer": "A", "explanation": "根据《航空法》第 X 条规定……"},
            direct_to_client=True,
        )

    registry.register("get_detail", get_detail, description="返回题目详情（原文直出）")


class DirectToClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_result_skips_llm_generation(self) -> None:
        registry = ToolRegistry()
        _register_direct_tool(registry)
        model = _Model()
        agent = ReActAgent(model, ToolExecutor(registry), system_prompt="测试", tool_definitions=[])
        runtime = AgentRuntime(agent, run_store=MemoryRunStore())

        context = await runtime.run("thread-direct-1", "题目详情")
        answer = _get_final_answer(context)

        # 答案是工具返回的结构化 JSON，不是 LLM 生成的文本。
        parsed = json.loads(answer)
        self.assertEqual(parsed["answer"], "A")
        self.assertEqual(parsed["explanation"], "根据《航空法》第 X 条规定……")
        started = [event for event in context.events if event.event_type == "tool_started"]
        finished = [event for event in context.events if event.event_type == "tool_finished"]
        self.assertEqual(started[0].payload["tool_call_id"], "call-1")
        self.assertEqual(finished[0].payload["tool_call_id"], "call-1")
        # LLM 只被调用了一次（发起工具调用），没有被用来生成最终回复。
        self.assertEqual(model.call_count, 1)

    async def test_normal_tool_still_goes_through_llm(self) -> None:
        registry = ToolRegistry()

        async def normal_tool(**_kwargs: object) -> ToolResult:
            return ToolResult(tool_name="normal_tool", success=True, value="普通结果")

        registry.register("normal_tool", normal_tool, description="普通工具")
        model = _Model()
        agent = ReActAgent(model, ToolExecutor(registry), system_prompt="测试", tool_definitions=[])
        runtime = AgentRuntime(agent, run_store=MemoryRunStore())

        context = await runtime.run("thread-direct-2", "普通查询")
        answer = _get_final_answer(context)

        # 没有 direct_to_client 声明时，LLM 会被再次调用来生成回复。
        self.assertEqual(answer, "LLM 二次生成的文本")
        self.assertEqual(model.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class _ActionModel:
    """第一轮发起工具调用，第二轮返回文本。"""

    def __init__(self) -> None:
        self.call_count = 0

    async def chat(self, _messages: object, *, tools: object = None) -> object:
        del tools
        self.call_count += 1
        if self.call_count == 1:
            return assistant_message(
                "", tool_calls=[{"id": "call-1", "name": "emit_action", "args": {}}]
            )
        return assistant_message("完成")

    async def stream_chat(self, _messages: object, *, tools: object = None) -> AsyncIterator[StreamChunk]:
        del tools
        yield StreamChunk(text="完成")


class ClientActionMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_metadata_client_actions_are_collected(self) -> None:
        """通用 metadata.client_actions 应进入 RunContext.state，供 API 下发前端。"""
        registry = ToolRegistry()

        async def emit_action(**_kwargs: object) -> ToolResult:
            return ToolResult(
                tool_name="emit_action",
                success=True,
                value="已生成候选",
                metadata={
                    "client_actions": [
                        {"type": "SHOW_PLACE_CANDIDATES", "query": "测试地点甲"}
                    ]
                },
            )

        registry.register("emit_action", emit_action, description="下发前端动作")
        runtime = AgentRuntime(
            ReActAgent(
                _ActionModel(),
                ToolExecutor(registry),
                system_prompt="测试",
                tool_definitions=[],
            ),
            run_store=MemoryRunStore(),
        )

        context = await runtime.run("thread-action-1", "测试地点甲")

        self.assertEqual(
            context.state.get("client_actions"),
            [{"type": "SHOW_PLACE_CANDIDATES", "query": "测试地点甲"}],
        )
