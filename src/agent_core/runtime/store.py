"""Runtime 的默认内存实现。

它只适合测试和单进程开发环境。生产应用应通过 ``RunStore`` 注入 SQLite、
PostgreSQL 或其他持久化实现。
"""

from __future__ import annotations

import copy

from agent_core.protocol.runtime import RunContext
from agent_core.ports import RunStore


class MemoryRunStore(RunStore):
    """带基本版本控制和幂等查询的内存运行存储。"""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], dict] = {}

    async def save_run(self, context: RunContext) -> None:
        key = (context.thread_id, context.run_id)
        previous = self._runs.get(key)
        previous_version = int(previous.get("version", 0)) if previous else 0
        if previous is not None and context.version != previous_version:
            raise RuntimeError(f"run {context.run_id} 版本冲突")
        context.version = previous_version + 1
        self._runs[key] = copy.deepcopy(context.to_dict())

    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None:
        value = self._runs.get((thread_id, run_id))
        if value is None:
            return None
        return _context_from_dict(value)

    async def find_run_by_idempotency(
        self,
        tenant_id: str | None,
        user_id: str | None,
        idempotency_key: str,
    ) -> RunContext | None:
        for value in self._runs.values():
            if (
                value.get("tenant_id") == tenant_id
                and value.get("user_id") == user_id
                and value.get("idempotency_key") == idempotency_key
            ):
                return _context_from_dict(value)
        return None


def _context_from_dict(value: dict) -> RunContext:
    """从持久化字典恢复 Core 上下文，忽略不可序列化的回调。"""
    context = RunContext(
        thread_id=str(value["thread_id"]),
        run_id=str(value["run_id"]),
        user_id=value.get("user_id"),
        tenant_id=value.get("tenant_id"),
        status=value.get("status", "queued"),
        version=int(value.get("version", 0)),
        idempotency_key=value.get("idempotency_key"),
        iteration=int(value.get("iteration", 0)),
        tool_calls_used=int(value.get("tool_calls_used", 0)),
        pending_clarification=value.get("pending_clarification"),
        state=copy.deepcopy(value.get("state") or {}),
        metadata=copy.deepcopy(value.get("metadata") or {}),
        checkpoint=copy.deepcopy(value.get("checkpoint") or {}),
    )
    pending_approval = value.get("pending_approval")
    if isinstance(pending_approval, dict):
        from agent_core.protocol.runtime import ApprovalRecord

        context.pending_approval = ApprovalRecord(
            approval_id=str(pending_approval.get("approval_id", "")),
            thread_id=str(pending_approval.get("thread_id", context.thread_id)),
            run_id=str(pending_approval.get("run_id", context.run_id)),
            action=str(pending_approval.get("action", "")),
            arguments=dict(pending_approval.get("arguments") or {}),
            user_id=pending_approval.get("user_id"),
            tenant_id=pending_approval.get("tenant_id"),
            status=pending_approval.get("status", "pending"),
            reason=pending_approval.get("reason"),
            created_at=str(pending_approval.get("created_at", "")),
            expires_at=pending_approval.get("expires_at"),
        )
    return context
