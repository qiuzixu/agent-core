"""Agent Core 的独立回归测试。

这些测试只依赖标准库和 Core 自身，确保 Core 不会反向依赖业务项目或具体模型 SDK。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agent_core import (
    AgentRuntime,
    MemoryCheckpointer,
    MemoryEventSink,
    MemoryRunStore,
    ReActAgent,
    RunContext,
    StreamChunk,
    ModelProviderRegistry,
    create_model_provider,
    ToolExecutor,
    ToolRegistry,
    assistant_message,
)
from agent_core.access import AccessContext
from agent_core.protocol import user_message
from agent_core.storage import (
    SqliteContextStore,
    SqliteRuntimeStore,
    SqliteSessionStore,
    SqliteWorkflowExecutionStore,
    WorkflowExecution,
)


def _tool_call(call_id: str, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造 Core 内部使用的扁平工具调用格式。"""
    return {
        "id": call_id,
        "type": "function",
        "name": name,
        "args": args or {},
    }


class _FakeModel:
    """同时实现同步响应和流式响应的最小模型适配器。"""

    def __init__(
        self,
        responses: list[Any] | None = None,
        streams: list[list[StreamChunk]] | None = None,
        first_chunk_ready: asyncio.Event | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.streams = list(streams or [])
        self.first_chunk_ready = first_chunk_ready

    async def chat(self, _messages: Any, *, tools: Any = None) -> Any:
        del tools
        return self.responses.pop(0)

    async def stream_chat(self, _messages: Any, *, tools: Any = None) -> AsyncIterator[StreamChunk]:
        del tools
        chunks = self.streams.pop(0)
        for index, chunk in enumerate(chunks):
            yield chunk
            if index == 0 and self.first_chunk_ready is not None:
                await self.first_chunk_ready.wait()


def _make_agent(
    model: _FakeModel,
    *,
    checkpointer: MemoryCheckpointer | None = None,
    max_iterations: int = 5,
) -> ReActAgent:
    registry = ToolRegistry()
    registry.register(
        "lookup",
        lambda query: f"result:{query}",
        "查询数据",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    return ReActAgent(
        model,
        ToolExecutor(registry),
        system_prompt="测试助手",
        tool_definitions=registry.build_tool_definitions(),
        checkpointer=checkpointer,
        max_iterations=max_iterations,
    )


class TestAgentCorePublicApi(unittest.IsolatedAsyncioTestCase):
    def test_model_provider_registry_is_sdk_agnostic(self) -> None:
        """Core 只负责注册和选择，不需要导入具体模型 SDK。"""
        registry = ModelProviderRegistry()

        def build_fake(config: Any, *, model: str | None = None) -> dict[str, Any]:
            return {"provider": config.llm_provider, "model": model or config.model_name}

        registry.register("Fake", build_fake)
        config = type("Config", (), {"llm_provider": "fake", "model_name": "demo"})()
        self.assertEqual(
            create_model_provider(config, registry=registry),
            {"provider": "fake", "model": "demo"},
        )
        self.assertEqual(registry.names(), ("fake",))
        with self.assertRaises(ValueError):
            registry.register("fake", build_fake)
        with self.assertRaises(ValueError):
            create_model_provider(
                type("Config", (), {"llm_provider": "missing"})(),
                registry=registry,
            )

    def test_builtin_model_providers_are_registered_in_core(self) -> None:
        """Core 内置 Provider 已注册，但导入时不要求安装对应 SDK。"""
        from agent_core.model import (
            AnthropicProvider,
            GeminiProvider,
            MODEL_PROVIDER_REGISTRY,
            OllamaProvider,
            OpenAIProvider,
        )

        self.assertEqual(
            MODEL_PROVIDER_REGISTRY.names(),
            ("anthropic", "gemini", "ollama", "openai"),
        )
        self.assertTrue(all(cls.__module__ == "agent_core.model.providers" for cls in (
            OpenAIProvider,
            AnthropicProvider,
            GeminiProvider,
            OllamaProvider,
        )))

    async def test_runtime_owns_lifecycle_and_publishes_events(self) -> None:
        model = _FakeModel(responses=[assistant_message("运行完成")])
        events = MemoryEventSink()
        runtime = AgentRuntime(
            _make_agent(model),
            run_store=MemoryRunStore(),
            event_sink=events,
        )

        context = await runtime.run(
            "thread-runtime",
            "开始运行",
            user_id="user-1",
            tenant_id="tenant-1",
        )

        self.assertEqual(context.status, "completed")
        self.assertEqual(context.user_id, "user-1")
        self.assertEqual(context.tenant_id, "tenant-1")
        self.assertEqual(
            [event.event_type for event in events.events],
            ["run_started", "model_finished", "run_completed"],
        )

    async def test_runtime_resume_uses_recoverable_checkpoint(self) -> None:
        model = _FakeModel(responses=[assistant_message("恢复完成")])
        store = MemoryRunStore()
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
        from agent_core.protocol.runtime import RunContext

        interrupted = RunContext(
            thread_id="thread-resume",
            run_id="run-resume",
            status="interrupted",
            checkpoint=checkpoint,
        )
        await store.save_run(interrupted)
        runtime = AgentRuntime(
            _make_agent(model),
            run_store=store,
        )
        resumed = await runtime.resume("thread-resume", interrupted.run_id)
        result = await runtime.wait("thread-resume", resumed.context.run_id)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.checkpoint["last_answer"], "恢复完成")

    async def test_run_supports_tool_call_and_persists_checkpoint(self) -> None:
        checkpointer = MemoryCheckpointer()
        model = _FakeModel(
            responses=[
                assistant_message(
                    "",
                    tool_calls=[_tool_call("call-1", "lookup", {"query": "机场"})],
                ),
                assistant_message("已查询完成"),
            ]
        )
        context = RunContext(thread_id="thread-core", run_id="run-core")

        agent = _make_agent(model, checkpointer=checkpointer)
        answer = await agent.run(
            "查询机场",
            run_context=context,
        )

        self.assertEqual(answer, "已查询完成")
        self.assertEqual(context.status, "completed")
        self.assertEqual(context.checkpoint["phase"], "completed")
        self.assertEqual(context.checkpoint["tool_calls_used"], 1)
        saved = await checkpointer.load("thread-core")
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved["run_context"]["run_id"], "run-core")
        self.assertEqual(await agent.get_checkpoint_history(), [])
        self.assertFalse(await agent.rollback_to("missing-version"))

    async def test_stream_yields_text_before_model_finishes(self) -> None:
        release = asyncio.Event()
        model = _FakeModel(
            streams=[[StreamChunk(text="第一块"), StreamChunk(text="第二块")]],
            first_chunk_ready=release,
        )
        context = RunContext(thread_id="thread-stream", run_id="run-stream")
        stream = _make_agent(model).stream("你好", run_context=context)

        self.assertEqual(await asyncio.wait_for(anext(stream), timeout=0.5), "第一块")
        self.assertEqual(context.status, "running")

        release.set()
        self.assertEqual(await anext(stream), "第二块")
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)
        self.assertEqual(context.status, "completed")

    def test_core_imports_without_site_packages(self) -> None:
        """Core 包的导入不能隐式要求 OpenAI、FastAPI 等第三方依赖。"""
        source_dir = str(Path(__file__).resolve().parents[1] / "src")
        env = os.environ.copy()
        env["PYTHONPATH"] = source_dir
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                "from agent_core import ReActAgent, RunContext; print(ReActAgent.__name__, RunContext.__name__)",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ReActAgent RunContext")

    async def test_sqlite_storage_is_available_from_core(self) -> None:
        """Core 内置 SQLite 存储不依赖业务包或第三方驱动。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"

            sessions = SqliteSessionStore(database)
            await sessions.append("thread-1", [user_message("你好")])
            self.assertEqual((await sessions.load_messages("thread-1"))[0].content, "你好")

            contexts = SqliteContextStore(database)
            await contexts.update(
                "thread-1",
                {"language": "zh-CN"},
                access=AccessContext(user_id="user-1", tenant_id="tenant-1"),
            )
            self.assertEqual((await contexts.get_context("thread-1"))["language"], "zh-CN")

            workflows = SqliteWorkflowExecutionStore(database)
            execution = WorkflowExecution(definition_id="demo", tenant_id="tenant-1")
            await workflows.save_execution(execution)
            self.assertEqual((await workflows.load_execution(execution.execution_id)).version, 1)

            runs = SqliteRuntimeStore(database)
            context = RunContext(thread_id="thread-1", run_id="run-1")
            await runs.save_run(context)
            self.assertEqual((await runs.load_run("thread-1", "run-1")).version, 1)


if __name__ == "__main__":
    unittest.main()
