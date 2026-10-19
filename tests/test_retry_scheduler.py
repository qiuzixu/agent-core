from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_core import AgentRuntime, ReActAgent, RunContext, ToolExecutor, ToolRegistry
from agent_core.protocol.messages import assistant_message
from agent_core.storage.runtime import MemoryRuntimeStore


class _EchoModel:
    async def chat(self, messages: list[Any], **kwargs: Any) -> Any:
        del messages, kwargs
        return assistant_message("model-answer")

    async def stream_chat(self, messages: list[Any], **kwargs: Any):
        yield assistant_message("model-answer")


class RetrySchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_due_retries_wakes_only_due_runs(self) -> None:
        store = MemoryRuntimeStore()
        registry = ToolRegistry()
        agent = ReActAgent(_EchoModel(), ToolExecutor(registry), system_prompt="s", tool_definitions=[])
        seen: list[str] = []

        async def executor(
            context: RunContext,
            user_input: str,
            *,
            memory_context: str | None = None,
            checkpoint_state: dict[str, Any] | None = None,
        ) -> str:
            seen.append(context.run_id)
            return "resumed"

        runtime = AgentRuntime(agent, run_store=store, run_executor=executor)

        due = RunContext(thread_id="t-due", run_id="r-due", status="waiting_retry")
        due.state["pending_retry"] = {
            "tool_name": "upstream",
            "next_retry_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        due.checkpoint = {"phase": "completed", "messages": []}
        await store.save_run(due)

        future = RunContext(thread_id="t-future", run_id="r-future", status="waiting_retry")
        future.state["pending_retry"] = {
            "tool_name": "upstream",
            "next_retry_at": (datetime.now(UTC) + timedelta(seconds=3600)).isoformat(),
        }
        future.checkpoint = {"phase": "completed", "messages": []}
        await store.save_run(future)

        resumed = await runtime.resume_due_retries()
        self.assertEqual([item.context.run_id for item in resumed], ["r-due"])
        await runtime.wait("t-due", "r-due")
        loaded = await store.load_run("t-due", "r-due")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.status, "completed")
        self.assertEqual(seen, ["r-due"])


if __name__ == "__main__":
    unittest.main()
