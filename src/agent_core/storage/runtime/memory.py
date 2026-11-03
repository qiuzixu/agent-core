"""进程内 RuntimeStore，主要用于测试与最小部署。"""

from __future__ import annotations

import asyncio
import copy
import asyncio

from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_core.protocol.runtime import (
    ApprovalRecord,
    ApprovalStatus,
    RunContext,
)
from agent_core.runtime.state import transition_run_status
from agent_core.storage._sqlite_utils import (
    _run_in_thread,
    run_sync_in_thread,
    sqlite_connection,
)
from agent_core.storage.runtime.base import (
    RuntimeConcurrencyError,
    RuntimeStore,
    _approval_from_dict,
    _context_from_dict,
)


class MemoryRuntimeStore(RuntimeStore):
    """测试用内存实现。"""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], dict[str, Any]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def save_run(self, context: RunContext) -> None:
        key = (context.thread_id, context.run_id)
        previous = self._runs.get(key)
        if previous is not None and int(previous.get("version", 0)) != context.version:
            raise RuntimeConcurrencyError(f"run {context.run_id} 版本冲突")
        context.version = (int(previous.get("version", 0)) + 1) if previous else 1
        self._runs[key] = copy.deepcopy(context.to_dict())
        if context.pending_approval:
            await self.save_approval(context.pending_approval)

    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None:
        value = self._runs.get((thread_id, run_id))
        return _context_from_dict(value) if value else None

    async def list_runs(self, thread_id: str) -> list[RunContext]:
        return [
            _context_from_dict(value)
            for (stored_thread, _), value in self._runs.items()
            if stored_thread == thread_id
        ]

    async def list_runs_by_status(self, status: str) -> list[RunContext]:
        return [
            _context_from_dict(value)
            for value in self._runs.values()
            if value.get("status") == status
        ]

    async def find_run_by_idempotency(
        self, tenant_id: str | None, user_id: str | None, idempotency_key: str
    ) -> RunContext | None:
        for value in self._runs.values():
            if (
                value.get("tenant_id") == tenant_id
                and value.get("user_id") == user_id
                and value.get("idempotency_key") == idempotency_key
            ):
                return _context_from_dict(value)
        return None

    async def mark_stale_runs(self) -> list[RunContext]:
        stale: list[RunContext] = []
        for value in list(self._runs.values()):
            if value.get("status") not in {"queued", "running"}:
                continue
            context = _context_from_dict(value)
            transition_run_status(context, "interrupted")
            context.emit("run_interrupted", reason="服务重启")
            context.version = int(value.get("version", 0))
            await self.save_run(context)
            stale.append(context)
        return stale

    async def save_thread_owner(self, thread_id: str, user_id: str, tenant_id: str) -> None:
        previous = self._threads.get(thread_id)
        if previous and previous != (user_id, tenant_id):
            raise RuntimeConcurrencyError("thread 归属冲突")
        self._threads[thread_id] = (user_id, tenant_id)

    async def get_thread_owner(self, thread_id: str) -> tuple[str, str] | None:
        return self._threads.get(thread_id)

    async def save_approval(self, approval: ApprovalRecord) -> None:
        async with self._lock:
            current = self._approvals.get(approval.approval_id)
            if current is not None and current.get("status") != "pending":
                return
            self._approvals[approval.approval_id] = copy.deepcopy(approval.to_dict())

    async def load_approval(self, approval_id: str) -> ApprovalRecord | None:
        return _approval_from_dict(copy.deepcopy(self._approvals.get(approval_id)))

    async def transition_approval(
        self,
        approval: ApprovalRecord,
        *,
        expected_status: ApprovalStatus = "pending",
    ) -> bool:
        async with self._lock:
            current = self._approvals.get(approval.approval_id)
            if current is None or current.get("status") != expected_status:
                return False
            self._approvals[approval.approval_id] = copy.deepcopy(approval.to_dict())
            return True

    async def list_approvals(
        self, *, status: str = "pending", limit: int = 100
    ) -> list[ApprovalRecord]:
        """按状态列出审批记录（跨进程可见的待审批列表来源）。"""
        async with self._lock:
            rows = [
                copy.deepcopy(value)
                for value in self._approvals.values()
                if value.get("status") == status
            ]
        records = [record for record in map(_approval_from_dict, rows) if record is not None]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records[:limit]

    async def purge_approvals(self, before: str) -> int:
        """清理早于 before 的终态审批记录（内存版：终态且决定时间早于基准）。"""
        terminal = {"approved", "rejected", "expired"}
        async with self._lock:
            stale = [
                approval_id
                for approval_id, value in self._approvals.items()
                if value.get("status") in terminal
                and (value.get("decided_at") or value.get("created_at") or "")
                <= before
            ]
            for approval_id in stale:
                self._approvals.pop(approval_id, None)
        return len(stale)


# ──────────────────────────────────────────────
# 3. SQLite 后端（开发环境）
# ──────────────────────────────────────────────
