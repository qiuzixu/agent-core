"""代码评审中高风险问题的回归测试。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from agent_core.access import AccessContext
from agent_core.checkpoint import FileCheckpointer
from agent_core.documents import Document
from agent_core.errors import CheckpointError, ModelInvocationError
from agent_core.guardrails import GuardrailsMiddleware, LengthGuard
from agent_core.hitl import ApprovalQueue, ApprovalStatus
from agent_core.middleware import MiddlewareAction, MiddlewareContext
from agent_core.model import GovernedModelAdapter, ModelExecutionPolicy, OpenAIProvider, StreamChunk
from agent_core.protocol import Message, RunContext, assistant_message, system_message, user_message
from agent_core.retrieval import InMemoryVectorStore, VectorQuery, VectorRecord
from agent_core.runtime import ReActAgent
from agent_core.storage import (
    MemoryRuntimeStore,
    SqliteRuntimeStore,
    SqliteWorkflowExecutionStore,
    WorkflowConcurrencyError,
    WorkflowExecution,
)
from agent_core.storage.context import MemoryContextStore
from agent_core.storage.runtime import RuntimeConcurrencyError
from agent_core.storage.workflow import _from_dict
from agent_core.tools import ToolExecutor, ToolRegistry


class _FailingModel:
    async def chat(self, messages: list[Message], **kwargs: Any) -> Message:
        del messages, kwargs
        raise ModelInvocationError("主模型不可用")

    async def stream_chat(self, messages: list[Message], **kwargs: Any):
        del messages, kwargs
        if False:
            yield StreamChunk()

    async def context_usage(self, messages: list[Message], **kwargs: Any) -> Any:
        del messages, kwargs
        return None


class _SuccessfulModel(_FailingModel):
    async def chat(self, messages: list[Message], **kwargs: Any) -> Message:
        del messages, kwargs
        return assistant_message("fallback-ok")


class _RecordingCheckpointer:
    def __init__(self) -> None:
        self.states: list[dict[str, Any]] = []

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> str:
        del thread_id, metadata
        self.states.append(state)
        return str(len(self.states))

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        del thread_id
        return self.states[-1] if self.states else None

    async def delete(self, thread_id: str) -> None:
        del thread_id
        self.states.clear()


class _ToolCallingModel(_SuccessfulModel):
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[Message], **kwargs: Any) -> Message:
        del messages, kwargs
        self.calls += 1
        if self.calls == 1:
            return assistant_message(
                "",
                tool_calls=[
                    {"id": "fast-1", "name": "fast", "args": {}},
                    {"id": "slow-1", "name": "slow", "args": {}},
                ],
            )
        return assistant_message("完成")


class _JsonProvider(OpenAIProvider):
    def __init__(self) -> None:
        self.seen_messages: list[Message] = []

    async def chat(self, messages: list[Message], **kwargs: Any) -> Message:
        del kwargs
        self.seen_messages = messages
        return assistant_message('{"ok": true}')


class TestReviewRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_runtime_rejects_stale_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteRuntimeStore(Path(directory) / "runtime.db")
            context = RunContext(thread_id="thread", run_id="run")
            await store.save_run(context)
            first = await store.load_run("thread", "run")
            stale = await store.load_run("thread", "run")
            assert first is not None and stale is not None
            first.status = "running"
            await store.save_run(first)
            stale.status = "failed"
            with self.assertRaises(RuntimeConcurrencyError):
                await store.save_run(stale)

    async def test_sqlite_runtime_does_not_advance_version_when_transaction_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteRuntimeStore(Path(directory) / "runtime.db")
            original_connect = store._connect

            @contextmanager
            def failing_connect() -> Iterator[Any]:
                with original_connect() as connection:
                    yield connection
                    raise OSError("commit failed")

            context = RunContext(thread_id="thread", run_id="run")
            with patch.object(store, "_connect", failing_connect), self.assertRaises(OSError):
                await store.save_run(context)
            self.assertEqual(context.version, 0)

    async def test_sqlite_workflow_rejects_stale_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteWorkflowExecutionStore(Path(directory) / "workflow.db")
            execution = WorkflowExecution(definition_id="demo")
            await store.save(execution)
            first = await store.load(execution.execution_id)
            stale = await store.load(execution.execution_id)
            assert first is not None and stale is not None
            first.status = "running"
            await store.save(first)
            stale.status = "failed"
            with self.assertRaises(WorkflowConcurrencyError):
                await store.save(stale)

    async def test_sqlite_workflow_does_not_advance_version_when_transaction_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteWorkflowExecutionStore(Path(directory) / "workflow.db")
            original_connect = store._connect

            @contextmanager
            def failing_connect() -> Iterator[Any]:
                with original_connect() as connection:
                    yield connection
                    raise OSError("commit failed")

            execution = WorkflowExecution(definition_id="demo")
            with patch.object(store, "_connect", failing_connect), self.assertRaises(OSError):
                await store.save(execution)
            self.assertEqual(execution.version, 0)

    def test_workflow_jsonb_string_fields_are_decoded(self) -> None:
        execution = _from_dict(
            {
                "execution_id": "exec-1",
                "input_data": '{"input": 1}',
                "result_data": '{"result": 2}',
                "events": '[{"event_type": "done"}]',
            }
        )
        self.assertEqual(execution.input_data, {"input": 1})
        self.assertEqual(execution.result_data, {"result": 2})
        self.assertEqual(execution.events, [{"event_type": "done"}])

    async def test_approval_timeout_is_persisted_and_cannot_overwrite_decision(self) -> None:
        store = MemoryRuntimeStore()
        queue = ApprovalQueue(store, poll_interval_seconds=0.001)
        request = await queue.create_request_async(
            "thread",
            "dangerous_tool",
            {},
            timeout_seconds=0.01,
        )
        self.assertIsNotNone(request.expires_at)
        self.assertEqual(await queue.wait_for_decision(request, timeout_seconds=0.01), ApprovalStatus.TIMEOUT)
        record = await store.load_approval(request.request_id)
        assert record is not None
        self.assertEqual(record.status, "expired")

        approved = request.to_record()
        approved.status = "approved"
        self.assertFalse(await store.transition_approval(approved, expected_status="pending"))
        record = await store.load_approval(request.request_id)
        assert record is not None
        self.assertEqual(record.status, "expired")

    async def test_file_checkpoint_failed_replace_keeps_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpointer = FileCheckpointer(directory)
            await checkpointer.save("thread", {"step": 1})
            with (
                patch("agent_core.checkpoint.store.os.replace", side_effect=OSError("disk error")),
                self.assertRaises(CheckpointError),
            ):
                await checkpointer.save("thread", {"step": 2})
            self.assertEqual(await checkpointer.load("thread"), {"step": 1})

    async def test_model_fallback_consumes_one_local_rate_limit_slot(self) -> None:
        governed = GovernedModelAdapter(
            _FailingModel(),
            fallbacks=[_SuccessfulModel()],
            policy=ModelExecutionPolicy(
                requests_per_window=1,
                window_seconds=60,
                queue_timeout_seconds=0.01,
            ),
        )
        result = await governed.chat([user_message("hello")])
        self.assertEqual(result.content, "fallback-ok")

    async def test_parse_json_does_not_mutate_input_messages(self) -> None:
        provider = _JsonProvider()
        messages = [system_message("original"), user_message("json")]
        result = await provider.parse_json(messages, schema={"type": "object", "required": ["ok"]})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(messages[0].content, "original")
        self.assertIn("有效的 JSON", provider.seen_messages[0].content)

    async def test_openai_bad_tool_arguments_are_wrapped(self) -> None:
        async def create(**kwargs: Any) -> Any:
            del kwargs
            tool_call = SimpleNamespace(
                id="call-1",
                function=SimpleNamespace(name="broken", arguments="{"),
            )
            message = SimpleNamespace(content="", tool_calls=[tool_call])
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")])

        provider = object.__new__(OpenAIProvider)
        provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        provider._model = "fake"
        provider._temperature = 0.0
        provider._max_tokens = 64
        with self.assertRaises(ModelInvocationError):
            await provider.chat([user_message("test")])

    async def test_length_guard_uses_input_and_output_limits_separately(self) -> None:
        middleware = GuardrailsMiddleware(
            input_guards=[LengthGuard(max_input_chars=3, max_output_chars=10)],
            output_guards=[LengthGuard(max_input_chars=3, max_output_chars=10)],
        )
        input_context = MiddlewareContext(messages=[user_message("1234")], iteration=0)
        self.assertEqual((await middleware.before_model(input_context)).action, MiddlewareAction.STOP)

        output_context = MiddlewareContext(messages=[user_message("ok")], iteration=0)
        output_context.llm_response = assistant_message("12345")
        self.assertEqual((await middleware.after_model(output_context)).action, MiddlewareAction.CONTINUE)

    async def test_context_access_owner_cannot_be_overwritten(self) -> None:
        store = MemoryContextStore(require_access=True)
        access = AccessContext(user_id="user-a", tenant_id="tenant-a")
        await store.update("thread", {"key": "value"}, access=access)
        with self.assertRaises(ValueError):
            await store.update(
                "thread",
                {"_access": {"user_id": "user-b", "tenant_id": "tenant-b"}},
                access=access,
            )

    async def test_vector_delete_validates_entire_batch_before_mutating(self) -> None:
        store = InMemoryVectorStore(require_access=True)
        owner_a = AccessContext(user_id="a", tenant_id="tenant")
        owner_b = AccessContext(user_id="b", tenant_id="tenant")
        records = [
            VectorRecord("a", (1.0, 0.0), Document("a"), user_id="a", tenant_id="tenant"),
            VectorRecord("b", (0.0, 1.0), Document("b"), user_id="b", tenant_id="tenant"),
        ]
        await store.upsert([records[0]], access=owner_a)
        await store.upsert([records[1]], access=owner_b)
        with self.assertRaises(PermissionError):
            await store.delete(["a", "b"], access=owner_a)
        matches = await store.search(VectorQuery((1.0, 0.0), limit=10, access=owner_a))
        self.assertEqual([match.record.record_id for match in matches], ["a"])

    async def test_react_checkpoints_each_completed_tool(self) -> None:
        async def fast() -> str:
            return "fast"

        async def slow() -> str:
            await asyncio.sleep(0.02)
            return "slow"

        registry = ToolRegistry()
        registry.register("fast", fast, "fast")
        registry.register("slow", slow, "slow")
        checkpointer = _RecordingCheckpointer()
        agent = ReActAgent(
            _ToolCallingModel(),
            ToolExecutor(registry),
            system_prompt="test",
            tool_definitions=registry.build_tool_definitions(),
            checkpointer=checkpointer,
        )
        self.assertEqual(await agent.run("go", thread_id="thread"), "完成")
        pending_sizes = [
            len(state.get("pending_tool_calls", []))
            for state in checkpointer.states
            if state.get("phase") == "execute_tools"
        ]
        self.assertIn(1, pending_sizes)


if __name__ == "__main__":
    unittest.main()
