"""Runtime 的默认内存实现。

它只适合测试和单进程开发环境。生产应用应通过 ``RunStore`` 注入 SQLite、
PostgreSQL 或其他持久化实现。
"""

from __future__ import annotations

import copy
from typing import Any

from agent_core.ports import RunStore
from agent_core.protocol.runtime import RunContext


class MemoryRunStore(RunStore):
    """带基本版本控制和幂等查询的内存运行存储。"""

    def __init__(self) -> None:
        # key=(thread_id, run_id)；值保存 RunContext 的可序列化快照。
        self._runs: dict[tuple[str, str], dict[str, Any]] = {}

    async def save_run(self, context: RunContext) -> None:
        # 乐观并发控制：写入方必须基于最新 version；旧值回写会被拒绝。
        key = (context.thread_id, context.run_id)
        previous = self._runs.get(key)
        previous_version = int(previous.get("version", 0)) if previous else 0
        if previous is not None and context.version != previous_version:
            raise RuntimeError(f"run {context.run_id} 版本冲突")
        context.version = previous_version + 1
        # deepcopy 隔离外部对象修改；加载方拿到的是存储快照，不是同一引用。
        self._runs[key] = copy.deepcopy(context.to_dict())

    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None:
        value = self._runs.get((thread_id, run_id))
        if value is None:
            return None
        return _context_from_dict(value)

    async def list_runs_by_status(self, status: str) -> list[RunContext]:
        """按状态扫描 run；O(n) 实现只适合测试和极小开发环境。"""
        return [
            _context_from_dict(value)
            for value in self._runs.values()
            if value.get("status") == status
        ]

    async def find_run_by_idempotency(
        self,
        tenant_id: str | None,
        user_id: str | None,
        idempotency_key: str,
    ) -> RunContext | None:
        # O(n) 扫描只适合测试/极小内存场景；正式后端应使用幂等键索引。
        for value in self._runs.values():
            if (
                value.get("tenant_id") == tenant_id
                and value.get("user_id") == user_id
                and value.get("idempotency_key") == idempotency_key
            ):
                return _context_from_dict(value)
        return None


def _context_from_dict(value: dict[str, Any]) -> RunContext:
    """从持久化字典恢复 Core 上下文，忽略不可序列化的回调。"""
    return RunContext.from_dict(copy.deepcopy(value))
