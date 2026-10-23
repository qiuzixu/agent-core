from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_core.ports import RecoverableRunStore
from agent_core.protocol.runtime import RunContext
from agent_core.runtime import (
    RunStateError,
    can_transition_run_status,
    transition_run_status,
)
from agent_core.runtime.service import AgentRuntime
from agent_core.runtime.store import MemoryRunStore


class StateMachineTests(unittest.IsolatedAsyncioTestCase):
    def test_same_status_is_idempotent(self) -> None:
        context = RunContext(thread_id="t", run_id="r", status="running")
        transition_run_status(context, "running")
        self.assertEqual(context.status, "running")

    def test_valid_transition_is_allowed(self) -> None:
        context = RunContext(thread_id="t", run_id="r")
        transition_run_status(context, "running")
        transition_run_status(context, "completed")
        self.assertTrue(can_transition_run_status("queued", "running"))
        self.assertEqual(context.status, "completed")

    def test_terminal_state_is_frozen(self) -> None:
        context = RunContext(thread_id="t", run_id="r", status="completed")
        with self.assertRaises(RunStateError):
            transition_run_status(context, "running")

    def test_interrupted_is_reachable_from_all_active_states(self) -> None:
        for status in ("queued", "running", "waiting_retry", "waiting_approval", "waiting_clarification"):
            self.assertTrue(can_transition_run_status(status, "interrupted"))


class _LegacyRunStore:
    """模拟只实现旧 RunStore 能力的第三方后端。"""

    async def save_run(self, context: RunContext) -> None: ...
    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None: ...
    async def find_run_by_idempotency(
        self,
        tenant_id: str | None,
        user_id: str | None,
        idempotency_key: str,
    ) -> RunContext | None: ...


class RecoverableRunStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_store_exposes_recovery_protocol(self) -> None:
        store = MemoryRunStore()
        self.assertIsInstance(store, RecoverableRunStore)
        context = RunContext(thread_id="t", run_id="r", status="waiting_retry")
        await store.save_run(context)
        runs = await store.list_runs_by_status("waiting_retry")
        self.assertEqual([item.run_id for item in runs], ["r"])

    async def test_runtime_ignores_legacy_store_for_retry_scan(self) -> None:
        class _Runtime:
            pass

        runtime = AgentRuntime(_Runtime(), run_store=_LegacyRunStore())
        self.assertEqual(await runtime.resume_due_retries(), [])

    async def test_runtime_uses_explicit_recoverable_store(self) -> None:
        class _Runtime:
            pass

        async def executor(*args: Any, **kwargs: Any) -> str:
            del args, kwargs
            return "ok"

        store = MemoryRunStore()
        runtime = AgentRuntime(_Runtime(), run_store=store, run_executor=executor)
        context = RunContext(thread_id="t", run_id="r", status="waiting_retry")
        context.state["pending_retry"] = {
            "tool_name": "upstream",
            "next_retry_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
        context.checkpoint = {"phase": "completed", "messages": []}
        await store.save_run(context)

        resumed = await runtime.resume_due_retries()
        self.assertEqual([item.context.run_id for item in resumed], ["r"])


if __name__ == "__main__":
    unittest.main()
