"""Agent Core 的独立回归测试。

这些测试只依赖标准库和 Core 自身，确保 Core 不会反向依赖业务项目或具体模型 SDK。
"""

from __future__ import annotations

import asyncio
import io
import json
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
    ApprovalQueue,
    DurableWorkflowRunner,
    MemoryCheckpointer,
    MemoryEventSink,
    MemoryRunLeaseStore,
    MemoryRunStore,
    StateMachineBuilder,
    ReActAgent,
    RunContext,
    StreamChunk,
    ModelProviderRegistry,
    create_model_provider,
    ToolExecutor,
    ToolRegistry,
    WorkflowPause,
    assistant_message,
)
from agent_core.access import AccessContext
from agent_core.acp import AcpSession, AcpStdioServer, AcpUpdate
from agent_core.protocol import user_message
from agent_core.storage import (
    MemoryContextStore,
    MemoryRuntimeStore,
    MemorySessionStore,
    MemoryWorkflowExecutionStore,
    SqliteContextStore,
    SqliteRunLeaseStore,
    SqliteRuntimeStore,
    SqliteSessionStore,
    RuntimeConcurrencyError,
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


class _FakeAcpBackend:
    """验证 ACP stdio 服务端的最小 Runtime 适配器。"""

    agent_name = "测试 Agent"
    agent_version = "1.0.0"
    supports_load_session = True

    async def create_session(
        self,
        session_id: str,
        cwd: str,
        _mcp_servers: list[dict[str, Any]],
    ) -> AcpSession:
        return AcpSession(session_id=session_id, cwd=cwd)

    async def load_session(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[dict[str, Any]],
    ) -> AcpSession:
        return await self.create_session(session_id, cwd, mcp_servers)

    async def prompt(
        self,
        _session: AcpSession,
        text: str,
        emit: Any,
        _request_permission: Any,
    ) -> dict[str, Any]:
        await emit(AcpUpdate.thought(f"正在处理：{text}"))
        await emit(AcpUpdate.text("处理完成"))
        return {"stopReason": "end_turn"}

    async def cancel(self, _session: AcpSession) -> None:
        return None


class _LeaseLossStore(MemoryRunLeaseStore):
    """首次心跳就模拟租约被其他 Worker 接管。"""

    async def renew(
        self,
        thread_id: str,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: float,
    ) -> bool:
        del thread_id, run_id, worker_id, ttl_seconds
        return False


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

    async def test_acp_stdio_emits_session_updates_and_final_result(self) -> None:
        """ACP 服务端能完成初始化、创建会话、流式更新和最终响应。"""
        output = io.StringIO()
        server = AcpStdioServer(_FakeAcpBackend(), stdin=io.StringIO(), stdout=output)

        await server._handle_request("initialize", "initialize", {"protocolVersion": 1})
        await server._handle_request("new", "session/new", {"cwd": "E:/workspace"})
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        session_id = records[-1]["result"]["sessionId"]

        await server._handle_request(
            "prompt",
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "查询机场"}],
            },
        )
        records = [json.loads(line) for line in output.getvalue().splitlines()]

        self.assertEqual(records[0]["result"]["protocolVersion"], 1)
        updates = [item for item in records if item.get("method") == "session/update"]
        self.assertEqual(
            [item["params"]["update"]["sessionUpdate"] for item in updates],
            ["agent_thought_chunk", "agent_message_chunk"],
        )
        self.assertEqual(records[-1]["result"], {"stopReason": "end_turn"})

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

    async def test_session_store_enforces_user_and_tenant_scope(self) -> None:
        """带访问身份写入的 Session 只能由所有者或同租户管理员读取。"""
        store = MemorySessionStore()
        owner = AccessContext(user_id="alice", tenant_id="tenant-a")
        other = AccessContext(user_id="bob", tenant_id="tenant-a")
        admin = AccessContext(
            user_id="admin",
            tenant_id="tenant-a",
            roles=frozenset({"tenant_admin"}),
        )

        await store.append("thread-secure", [user_message("私有消息")], access=owner)
        self.assertEqual((await store.load("thread-secure", access=owner))[0].content, "私有消息")
        self.assertEqual((await store.load("thread-secure", access=admin))[0].content, "私有消息")
        with self.assertRaises(PermissionError):
            await store.load("thread-secure", access=other)
        self.assertEqual(await store.list_threads(access=owner), ["thread-secure"])
        self.assertEqual(await store.list_threads(access=other), [])

    async def test_run_lease_prevents_concurrent_workers_and_allows_takeover(self) -> None:
        """未过期租约不可抢占，过期后另一个 Worker 可以接管。"""
        leases = MemoryRunLeaseStore()
        self.assertTrue(
            await leases.claim("thread", "run", "worker-a", ttl_seconds=0.02)
        )
        self.assertFalse(
            await leases.claim("thread", "run", "worker-b", ttl_seconds=1.0)
        )
        await asyncio.sleep(0.03)
        self.assertTrue(
            await leases.claim("thread", "run", "worker-b", ttl_seconds=1.0)
        )
        self.assertFalse(await leases.release("thread", "run", "worker-a"))
        self.assertTrue(await leases.release("thread", "run", "worker-b"))

    async def test_agent_runtime_claims_and_releases_run_lease(self) -> None:
        """AgentRuntime 执行期间持有租约，终态保存后释放。"""
        leases = MemoryRunLeaseStore()
        runtime = AgentRuntime(
            _make_agent(_FakeModel(responses=[assistant_message("完成")])),
            lease_store=leases,
            worker_id="runtime-worker",
            lease_seconds=1.0,
            heartbeat_seconds=0.1,
        )

        context = await runtime.run("thread-runtime-lease", "执行")
        self.assertEqual(context.status, "completed")
        self.assertTrue(
            await leases.claim(
                context.thread_id,
                context.run_id,
                "next-worker",
                ttl_seconds=1.0,
            )
        )

    async def test_agent_runtime_marks_run_interrupted_after_lease_loss(self) -> None:
        """心跳续租失败时停止执行，并把最终状态保存为 interrupted。"""

        class SlowModel(_FakeModel):
            async def chat(self, _messages: Any, *, tools: Any = None) -> Any:
                del _messages, tools
                await asyncio.sleep(1.0)
                return assistant_message("不应完成")

        run_store = MemoryRunStore()
        runtime = AgentRuntime(
            _make_agent(SlowModel()),
            run_store=run_store,
            lease_store=_LeaseLossStore(),
            worker_id="runtime-worker",
            lease_seconds=0.1,
            heartbeat_seconds=0.01,
        )
        handle = await runtime.start("thread-lease-loss", "执行")

        with self.assertRaises(RuntimeConcurrencyError):
            await runtime.wait(handle.context.thread_id, handle.context.run_id)
        stored = await run_store.load_run(handle.context.thread_id, handle.context.run_id)
        assert stored is not None
        self.assertEqual(stored.status, "interrupted")

    async def test_resume_releases_lease_when_run_save_fails(self) -> None:
        """恢复前保存 queued 状态失败时，不遗留阻塞其他 Worker 的租约。"""

        class FailingRunStore(MemoryRunStore):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next_save = False

            async def save_run(self, context: RunContext) -> None:
                if self.fail_next_save:
                    self.fail_next_save = False
                    raise RuntimeError("模拟保存失败")
                await super().save_run(context)

        run_store = FailingRunStore()
        leases = MemoryRunLeaseStore()
        context = RunContext(
            thread_id="thread-resume-failure",
            run_id="run-resume-failure",
            status="interrupted",
            checkpoint={"phase": "model"},
        )
        await run_store.save_run(context)
        run_store.fail_next_save = True
        runtime = AgentRuntime(
            _make_agent(_FakeModel()),
            run_store=run_store,
            lease_store=leases,
            worker_id="runtime-worker",
            lease_seconds=1.0,
            heartbeat_seconds=0.1,
        )

        with self.assertRaisesRegex(RuntimeError, "模拟保存失败"):
            await runtime.resume(context.thread_id, context.run_id)
        self.assertTrue(
            await leases.claim(
                context.thread_id,
                context.run_id,
                "next-worker",
                ttl_seconds=1.0,
            )
        )

    async def test_strict_access_mode_rejects_missing_identity(self) -> None:
        """生产严格模式不允许调用方遗漏用户和租户身份。"""
        owner = AccessContext(user_id="alice", tenant_id="tenant-a")

        sessions = MemorySessionStore(require_access=True)
        with self.assertRaises(PermissionError):
            await sessions.load("thread")
        await sessions.append("thread", [user_message("消息")], access=owner)

        contexts = MemoryContextStore(require_access=True)
        with self.assertRaises(PermissionError):
            await contexts.get("thread")
        self.assertEqual(
            (await contexts.update("thread", {"preference": "简洁"}, access=owner))["preference"],
            "简洁",
        )

        approvals = ApprovalQueue(require_access=True)
        with self.assertRaises(PermissionError):
            await approvals.create_request_async("thread", "tool", {})
        request = await approvals.create_request_async(
            "thread",
            "tool",
            {},
            user_id=owner.user_id,
            tenant_id=owner.tenant_id,
        )
        with self.assertRaises(PermissionError):
            await approvals.load_request(request.request_id)
        self.assertIs(
            await approvals.load_request(request.request_id, access=owner),
            request,
        )

        runtime = AgentRuntime(
            _make_agent(_FakeModel(responses=[assistant_message("完成")])),
            require_access=True,
        )
        with self.assertRaises(PermissionError):
            await runtime.start("thread", "执行")
        completed = await runtime.run(
            "thread",
            "执行",
            user_id=owner.user_id,
            tenant_id=owner.tenant_id,
        )
        with self.assertRaises(PermissionError):
            await runtime.get(completed.thread_id, completed.run_id)

    async def test_sqlite_run_lease_is_shared_between_store_instances(self) -> None:
        """SQLite 租约在多个 Store 实例间保持互斥。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "lease.db"
            first = SqliteRunLeaseStore(database)
            second = SqliteRunLeaseStore(database)
            self.assertTrue(
                await first.claim("thread", "run", "worker-a", ttl_seconds=1.0)
            )
            self.assertFalse(
                await second.claim("thread", "run", "worker-b", ttl_seconds=1.0)
            )
            self.assertTrue(await first.release("thread", "run", "worker-a"))
            self.assertTrue(
                await second.claim("thread", "run", "worker-b", ttl_seconds=1.0)
            )

    async def test_approval_can_be_resolved_by_another_queue_instance(self) -> None:
        """审批结果通过 Store 跨队列实例传播，不依赖同一个 asyncio.Event。"""
        store = MemoryRuntimeStore()
        waiting_queue = ApprovalQueue(store, poll_interval_seconds=0.01)
        deciding_queue = ApprovalQueue(store, poll_interval_seconds=0.01)
        request = await waiting_queue.create_request_async(
            "thread",
            "dangerous_tool",
            {"value": 1},
            user_id="alice",
            tenant_id="tenant-a",
        )
        waiter = asyncio.create_task(
            waiting_queue.wait_for_decision(request, timeout_seconds=1.0)
        )

        self.assertTrue(
            await deciding_queue.approve(
                request.request_id,
                access=AccessContext(user_id="alice", tenant_id="tenant-a"),
            )
        )
        self.assertEqual((await waiter).value, "approved")

    async def test_durable_workflow_resumes_from_interrupted_node(self) -> None:
        """工作流暂停后从已保存节点恢复，不重复之前完成的节点。"""
        calls = {"prepare": 0, "approve": 0}
        should_pause = True

        async def prepare(_state: dict[str, Any]) -> dict[str, Any]:
            calls["prepare"] += 1
            return {"prepared": True}

        async def approve(_state: dict[str, Any]) -> dict[str, Any]:
            nonlocal should_pause
            calls["approve"] += 1
            if should_pause:
                should_pause = False
                raise WorkflowPause("等待批准", {"approval_id": "approval-1"})
            return {"approved": True}

        machine = (
            StateMachineBuilder()
            .add_node("prepare", prepare)
            .add_node("approve", approve)
            .add_edge("__start__", "prepare")
            .add_edge("prepare", "approve")
            .add_edge("approve", "__end__")
            .build()
        )
        store = MemoryWorkflowExecutionStore()
        runner = DurableWorkflowRunner(machine, store)
        execution = await runner.start(
            "demo",
            {"input": "value"},
            access=AccessContext(user_id="alice", tenant_id="tenant-a"),
        )

        self.assertEqual(execution.status, "interrupted")
        self.assertEqual(execution.current_step, "approve")
        completed = await runner.resume(
            execution.execution_id,
            access=AccessContext(user_id="alice", tenant_id="tenant-a"),
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.result_data["approved"], True)
        self.assertEqual(calls, {"prepare": 1, "approve": 2})


if __name__ == "__main__":
    unittest.main()
