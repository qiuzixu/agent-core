"""Agent Core 新增通用能力的回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_core import (
    ChatPromptTemplate,
    Checkpointer,
    DurableWorkflowRunner,
    GovernedModelAdapter,
    InMemoryMemoryStore,
    MemoryConflictError,
    MemoryRecord,
    MessagesPlaceholder,
    MessageTemplate,
    ModelExecutionPolicy,
    ModelUnavailableError,
    NodeExecutionPolicy,
    PromptRegistry,
    PromptTemplate,
    SqliteMemoryStore,
    StateMachineBuilder,
    StructuredOutputSpec,
    TimeTravelCheckpointer,
    assistant_message,
    chat_structured,
    current_node_execution,
    model_execution_scope,
    user_message,
)
from agent_core.access import AccessContext
from agent_core.model import ContextUsage, StreamChunk
from agent_core.storage import MemoryWorkflowExecutionStore


class _Model:
    def __init__(self, responses: list[Any] | None = None, *, error: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls = 0
        self.received_messages: list[list[Any]] = []

    async def chat(
        self,
        _messages: Any,
        *,
        tools: Any = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        del tools, temperature, max_tokens
        self.calls += 1
        self.received_messages.append(list(_messages))
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)

    async def stream_chat(
        self,
        _messages: Any,
        *,
        tools: Any = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        del tools, temperature, max_tokens
        response = await self.chat(_messages)
        yield StreamChunk(text=response.content)

    async def context_usage(self, _messages: Any, *, tools: Any = None) -> ContextUsage:
        del tools
        return ContextUsage(input_tokens=1, context_window_tokens=100, exact=True, source="test")


class StructuredOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_output_retries_and_decodes(self) -> None:
        model = _Model([assistant_message("not-json"), assistant_message('{"name":"demo","count":2}')])
        spec = StructuredOutputSpec[tuple[str, int]](
            name="result",
            schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer", "minimum": 1},
                },
                "required": ["name", "count"],
                "additionalProperties": False,
            },
            decoder=lambda value: (value["name"], value["count"]),
            max_retries=1,
        )

        result = await chat_structured(
            model,
            [MessageTemplate("system", "你是业务助手").render(), user_message("返回结果")],
            spec,
        )

        self.assertEqual(result.value, ("demo", 2))
        self.assertEqual(result.attempts, 2)
        self.assertEqual(model.calls, 2)
        system_messages = [item for item in model.received_messages[0] if item.role == "system"]
        self.assertEqual(len(system_messages), 1)
        self.assertIn("你是业务助手", system_messages[0].content)
        self.assertIn("JSON Schema", system_messages[0].content)


class ModelGovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_and_circuit_breaker(self) -> None:
        primary = _Model(error=ModelUnavailableError("primary down"))
        fallback = _Model([assistant_message("fallback-1"), assistant_message("fallback-2")])
        model = GovernedModelAdapter(
            primary,
            fallbacks=[fallback],
            policy=ModelExecutionPolicy(failure_threshold=1, recovery_timeout_seconds=60),
        )

        with model_execution_scope("tenant-a"):
            first = await model.chat([user_message("one")])
            second = await model.chat([user_message("two")])

        self.assertEqual(first.content, "fallback-1")
        self.assertEqual(second.content, "fallback-2")
        self.assertEqual(primary.calls, 1)


class PromptTemplateTests(unittest.TestCase):
    def test_chat_template_supports_partial_and_message_placeholder(self) -> None:
        system = PromptTemplate("你是 {role}，语言为 {language}").partial(language="中文")
        template = ChatPromptTemplate(
            (
                MessageTemplate("system", system),
                MessagesPlaceholder("history", optional=True),
                MessageTemplate("user", "问题：{question}"),
            )
        )

        messages = template.render(
            role="助手",
            question="状态如何",
            history=[assistant_message("历史")],
        )

        self.assertEqual([item.role for item in messages], ["system", "assistant", "user"])
        self.assertEqual(messages[0].content, "你是 助手，语言为 中文")

    def test_template_rejects_attribute_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "简单变量名"):
            PromptTemplate("{user.name}")
        with self.assertRaisesRegex(ValueError, "不允许嵌套变量"):
            PromptTemplate("{value:{user_width}}")

    def test_registry_persists_default_variables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prompts.json"
            registry = PromptRegistry(path)
            registry.register("welcome", "{greeting}，{name}", defaults={"greeting": "你好"})

            reloaded = PromptRegistry(path)

            self.assertEqual(reloaded.render("welcome", name="Alice"), "你好，Alice")
            self.assertEqual(reloaded.get_history("welcome")[0]["variables"], ["greeting", "name"])


class TimeTravelCheckpointerTests(unittest.IsolatedAsyncioTestCase):
    async def test_rollback_pointer_survives_restart_and_delete_clears_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage_dir = Path(directory)
            checkpointer: Checkpointer = TimeTravelCheckpointer(storage_dir)
            first_version = await checkpointer.save("thread-1", {"step": 1})
            await checkpointer.save("thread-1", {"step": 2})

            self.assertIsInstance(first_version, str)
            assert isinstance(first_version, str)
            self.assertTrue(
                await TimeTravelCheckpointer(storage_dir).rollback("thread-1", first_version)
            )

            restarted: Checkpointer = TimeTravelCheckpointer(storage_dir)
            self.assertEqual(await restarted.load("thread-1"), {"step": 1})

            await restarted.delete("thread-1")
            self.assertIsNone(await TimeTravelCheckpointer(storage_dir).load("thread-1"))


class LongTermMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_access_search_ttl_and_conflict(self) -> None:
        store = InMemoryMemoryStore(require_access=True)
        alice = AccessContext(user_id="alice", tenant_id="tenant-a")
        bob = AccessContext(user_id="bob", tenant_id="tenant-a")
        record = await store.put(
            MemoryRecord(
                namespace="preferences",
                content="偏好使用中文回答",
                metadata={"kind": "preference"},
                source="conversation",
            ),
            access=alice,
        )

        results = await store.search(
            "preferences",
            query="中文",
            metadata={"kind": "preference"},
            access=alice,
        )
        self.assertEqual(results[0].record.memory_id, record.memory_id)
        with self.assertRaises(PermissionError):
            await store.get(record.memory_id, access=bob)
        with self.assertRaises(MemoryConflictError):
            await store.put(record, expected_version=0, access=alice)

        expired = await store.put(
            MemoryRecord(
                namespace="temporary",
                content="过期",
                expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            ),
            access=alice,
        )
        self.assertIsNone(await store.get(expired.memory_id, access=alice))

    async def test_sqlite_memory_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.db"
            first = SqliteMemoryStore(path)
            record = await first.put(MemoryRecord(namespace="facts", content="机场代码 ZBAA"))
            second = SqliteMemoryStore(path)

            loaded = await second.get(record.memory_id)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.content, "机场代码 ZBAA")
            self.assertEqual(loaded.version, 1)


class WorkflowPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_reuses_idempotency_key_and_persists_pending_event(self) -> None:
        contexts: list[tuple[int, str]] = []

        async def flaky(_state: dict[str, Any]) -> dict[str, Any]:
            context = current_node_execution()
            assert context is not None
            contexts.append((context.attempt, context.idempotency_key))
            if context.attempt == 1:
                raise RuntimeError("temporary")
            return {"ok": True}

        machine = (
            StateMachineBuilder()
            .add_node(
                "flaky",
                flaky,
                policy=NodeExecutionPolicy(max_attempts=2, idempotent=True),
            )
            .add_edge("__start__", "flaky")
            .add_edge("flaky", "__end__")
            .build()
        )
        runner = DurableWorkflowRunner(machine, MemoryWorkflowExecutionStore())

        result = await runner.start("reliable", {})

        self.assertEqual(result.status, "completed")
        self.assertEqual(contexts[0][1], contexts[1][1])
        pending = [event for event in result.events if event["event_type"] == "workflow_step_pending"]
        self.assertEqual([event["attempt"] for event in pending], [1, 2])
        self.assertIn("flaky", machine.to_mermaid())
        self.assertEqual(machine.describe()["nodes"][0]["policy"]["max_attempts"], 2)

    async def test_retry_requires_idempotent_node(self) -> None:
        with self.assertRaisesRegex(ValueError, "idempotent=True"):
            NodeExecutionPolicy(max_attempts=2)

    async def test_durable_runner_can_return_persisted_failure(self) -> None:
        async def fail(_state: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("业务步骤失败")

        machine = (
            StateMachineBuilder()
            .add_node("fail", fail)
            .add_edge("__start__", "fail")
            .add_edge("fail", "__end__")
            .build()
        )
        store = MemoryWorkflowExecutionStore()
        runner = DurableWorkflowRunner(machine, store, raise_on_failure=False)

        execution = await runner.start("failure-demo", {})

        self.assertEqual(execution.status, "failed")
        self.assertEqual(execution.current_step, "fail")
        persisted = await store.load(execution.execution_id)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.events[-1]["event_type"], "workflow_failed")


if __name__ == "__main__":
    unittest.main()
