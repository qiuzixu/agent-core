"""运行时存储协议、并发异常与领域对象转换工具。"""

from __future__ import annotations

import copy
import json
import sqlite3
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


# 运行时并发错误
# ————————————————————————————————————#
class RuntimeConcurrencyError(RuntimeError):
    """运行状态版本冲突，调用方应重新加载后再决定是否重试。"""


# ————————————————————————————————————#
# 运行时存储接口
# ————————————————————————————————————#
class RuntimeStore(ABC):
    """运行状态、事件和审批记录的持久化抽象。"""

    # ————————————————————————————————————#
    # 运行状态存储
    # ————————————————————————————————————#
    @abstractmethod
    async def save_run(self, context: RunContext) -> None: ...

    # ————————————————————————————————————#
    # 运行事件查询
    # ————————————————————————————————————#
    @abstractmethod
    async def find_run_by_idempotency(
        self, tenant_id: str | None, user_id: str | None, idempotency_key: str
    ) -> RunContext | None: ...

    # ————————————————————————————————————#
    # 运行状态查询
    # ————————————————————————————————————#
    @abstractmethod
    async def mark_stale_runs(self) -> list[RunContext]: ...

    # ————————————————————————————————————#
    # 运行状态查询
    # ————————————————————————————————————#
    @abstractmethod
    async def save_thread_owner(self, thread_id: str, user_id: str, tenant_id: str) -> None: ...

    # ————————————————————————————————————#
    # 运行状态查询
    # ————————————————————————————————————#
    @abstractmethod
    async def get_thread_owner(self, thread_id: str) -> tuple[str, str] | None: ...

    # ————————————————————————————————————#
    # 运行状态加载
    # ————————————————————————————————————#
    @abstractmethod
    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None: ...

    @abstractmethod
    async def list_runs(self, thread_id: str) -> list[RunContext]: ...

    @abstractmethod
    async def save_approval(self, approval: ApprovalRecord) -> None: ...

    @abstractmethod
    async def load_approval(self, approval_id: str) -> ApprovalRecord | None: ...

    @abstractmethod
    async def transition_approval(
        self,
        approval: ApprovalRecord,
        *,
        expected_status: ApprovalStatus = "pending",
    ) -> bool: ...

    async def purge_approvals(self, before: str) -> int:
        """清理早于 before（ISO 串）的终态审批记录；pending 永不清理。

        返回删除的行数；实现可选（队列侧用 getattr 探测，缺失时诚实跳过）。
        """
        return 0

    # ————————————————————————————————————#
    # 运行审批记录加载
    # ————————————————————————————————————#


def _approval_from_dict(value: dict[str, Any] | None) -> ApprovalRecord | None:
    if not value:
        return None
    arguments = value.get("arguments") or {}
    if isinstance(arguments, str):  # asyncpg 默认把 JSONB 返回为字符串，统一转换后再重建领域对象。
        arguments = json.loads(arguments) if arguments else {}  # 确保 arguments 是字典类型
    return ApprovalRecord.from_dict({**value, "arguments": dict(arguments)})


# ──────────────────────────────────────────────
# 数据库行 → RunContext
# ──────────────────────────────────────────────
def _context_from_dict(value: dict[str, Any]) -> RunContext:
    value = copy.deepcopy(value)
    # asyncpg 默认把 JSONB 返回为字符串，统一转换后再重建领域对象。
    for key in (
        "tags",
        "pending_approval",
        "pending_clarification",
        "state",
        "metadata",
        "checkpoint",
        "events",
    ):
        if isinstance(value.get(key), str):
            value[key] = json.loads(value[key])
    return RunContext.from_dict(value)


# ──────────────────────────────────────────────
# 2. 内存后端（测试环境）
# ──────────────────────────────────────────────
