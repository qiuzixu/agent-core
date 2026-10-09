"""run / stream 共享内核的行为一致性测试。

ReActAgent 的 ``run`` 与 ``stream`` 共用 ``_iterate`` 内核；本文件把
"两条路径产出相同的循环状态"固化为契约：相同模型脚本下，最终 checkpoint
的每个状态字段必须一致（messages / iteration / next_iteration /
last_answer / tool_calls_used / phase / pending_tool_calls）。
只用内存实现，不触碰 tempfile 与 sqlite。
"""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from typing import Any

from agent_core import (
    MemoryCheckpointer,
    ReActAgent,
    StreamChunk,
    ToolExecutor,
    ToolRegistry,
    assistant_message,
)
from agent_core.protocol import Message, RunContext


class _ScriptedModel:
    """chat 与 stream 输出完全一致的脚本模型。

    脚本每轮为 (text, tool_calls)；流式路径把文本按两个增量块发送，
    以覆盖"文本 + 工具调用"与"纯文本收尾"两类轮次。
    """

    def __init__(self, script: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self._script = list(script)
        self.chat_calls = 0
        self.stream_calls = 0

    async def chat(self, messages: list[Message], **kwargs: Any) -> Message:
        del messages, kwargs
        self.chat_calls += 1
        text, tool_calls = self._script.pop(0)
        return assistant_message(text, tool_calls=tool_calls)

    async def stream_chat(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        del messages, kwargs
        self.stream_calls += 1
        text, tool_calls = self._script.pop(0)
        if text:
            # 拆成两个增量块，验证逐块转发不改变归并结果。
            half = max(len(text) // 2, 1)
            yield StreamChunk(text=text[:half])
            yield StreamChunk(text=text[half:])
        if tool_calls:
            yield StreamChunk(tool_calls=tool_calls, is_tool_call=True)
        else:
            yield StreamChunk(finish_reason="stop")

    async def context_usage(self, messages: list[Message], **kwargs: Any) -> Any:
        del messages, kwargs
        return None


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "name": name, "args": args}


def _make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register("lookup", lambda query: f"result:{query}", "查询数据")
    registry.register("echo", lambda text: text, "原样返回")
    return registry


def _strip_run_identity(state: dict[str, Any] | None) -> dict[str, Any]:
    """去掉与身份相关的字段，只保留循环状态字段。"""
    assert state is not None
    return {
        "messages": state["messages"],
        "iteration": state["iteration"],
        "next_iteration": state["next_iteration"],
        "last_answer": state["last_answer"],
        "tool_calls_used": state["tool_calls_used"],
        "phase": state["phase"],
        "pending_tool_calls": state["pending_tool_calls"],
    }


class TestRunStreamCheckpointParity(unittest.IsolatedAsyncioTestCase):
    async def test_run_and_stream_produce_identical_checkpoint_state(self) -> None:
        """同一脚本下 run 与 stream 的最终 checkpoint 状态字段逐项一致。"""
        script = [
            ("先查一下", [_tool_call("call-1", "lookup", {"query": "机场"})]),
            ("再确认", [_tool_call("call-2", "echo", {"text": "ok"})]),
            ("全部完成", []),
        ]
        run_checkpointer = MemoryCheckpointer()
        stream_checkpointer = MemoryCheckpointer()
        run_model = _ScriptedModel(list(script))
        stream_model = _ScriptedModel(list(script))

        run_agent = ReActAgent(
            run_model,
            ToolExecutor(_make_registry()),
            system_prompt="测试助手",
            tool_definitions=_make_registry().build_tool_definitions(),
            checkpointer=run_checkpointer,
        )
        stream_agent = ReActAgent(
            stream_model,
            ToolExecutor(_make_registry()),
            system_prompt="测试助手",
            tool_definitions=_make_registry().build_tool_definitions(),
            checkpointer=stream_checkpointer,
        )

        answer = await run_agent.run("查询机场", thread_id="parity-run")
        self.assertEqual(answer, "全部完成")

        chunks: list[str] = []
        async for chunk in stream_agent.stream("查询机场", thread_id="parity-stream"):
            chunks.append(chunk)
        self.assertEqual("".join(chunks), "先查一下再确认全部完成")

        # 两条路径的每个 checkpoint 状态字段一致（含中间阶段与最终阶段）。
        self.assertEqual(
            _strip_run_identity(await run_checkpointer.load("parity-run")),
            _strip_run_identity(await stream_checkpointer.load("parity-stream")),
        )
        # 最终阶段：completed、无 pending 工具、预算一致。
        final_run = await run_checkpointer.load("parity-run")
        assert final_run is not None
        self.assertEqual(final_run["phase"], "completed")
        self.assertEqual(final_run["tool_calls_used"], 2)
        # 最终轮在 iteration=2 执行，下一轮指针为 3。
        self.assertEqual(final_run["next_iteration"], 3)

    async def test_resumed_run_matches_stream_recovery_state(self) -> None:
        """以 checkpoint 恢复时，run 与 stream 恢复出的状态同样一致。"""
        calls = [_tool_call("call-1", "lookup", {"query": "机场"})]
        checkpoint = {
            "messages": [
                {"role": "system", "content": "测试助手"},
                {"role": "user", "content": "查询机场"},
                assistant_message("中间结果", tool_calls=calls).to_dict(),
            ],
            "iteration": 0,
            "next_iteration": 0,
            "last_answer": "中间结果",
            "tool_calls_used": 1,
            "phase": "execute_tools",
            "pending_tool_calls": calls,
        }
        run_model = _ScriptedModel([("恢复完成", [])])
        stream_model = _ScriptedModel([("恢复完成", [])])
        run_checkpointer = MemoryCheckpointer()
        stream_checkpointer = MemoryCheckpointer()

        run_agent = ReActAgent(
            run_model,
            ToolExecutor(_make_registry()),
            system_prompt="测试助手",
            tool_definitions=_make_registry().build_tool_definitions(),
            checkpointer=run_checkpointer,
        )
        stream_agent = ReActAgent(
            stream_model,
            ToolExecutor(_make_registry()),
            system_prompt="测试助手",
            tool_definitions=_make_registry().build_tool_definitions(),
            checkpointer=stream_checkpointer,
        )

        run_context = RunContext(thread_id="resume-run", run_id="run-1")
        answer = await run_agent.run(
            "",
            run_context=run_context,
            checkpoint_state=checkpoint,
        )
        self.assertEqual(answer, "恢复完成")

        stream_context = RunContext(thread_id="resume-stream", run_id="run-2")
        chunks = [
            chunk
            async for chunk in stream_agent.stream(
                "",
                run_context=stream_context,
                checkpoint_state=checkpoint,
            )
        ]
        # 流式把同一回答拆成多个增量块，拼接后与 run 的最终回答一致。
        self.assertEqual("".join(chunks), "恢复完成")

        self.assertEqual(
            _strip_run_identity(await run_checkpointer.load("resume-run")),
            _strip_run_identity(await stream_checkpointer.load("resume-stream")),
        )
        # 两条路径对已完成的 pending 工具都只重放一次（不再重复执行 lookup）。
        self.assertEqual(run_model.chat_calls, 1)
        self.assertEqual(stream_model.stream_calls, 1)
        self.assertEqual(run_context.checkpoint["tool_calls_used"], 1)
        self.assertEqual(stream_context.checkpoint["tool_calls_used"], 1)


if __name__ == "__main__":
    unittest.main()
