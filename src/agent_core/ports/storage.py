"""Agent Core 的可替换存储端口。

协议描述运行时需要的最小能力；内置 SQLite/PostgreSQL 实现位于
``agent_core.storage``，Redis 或业务自己的存储也可以实现这些协议。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from agent_core.protocol.messages import Message
from agent_core.protocol.runtime import ApprovalRecord, RunContext, RunEvent
from agent_core.storage.model_selection import ModelSelection, ModelSelectionScope


class ModelSelectionStore(Protocol):
    """按用户/租户保存和解析有效模型的存储端口。"""

    async def resolve(self, *, user_id: str | None, tenant_id: str | None) -> ModelSelection | None:
        ...

    async def save(
        self,
        provider: str,
        model: str,
        *,
        scope: ModelSelectionScope,
        user_id: str | None,
        tenant_id: str | None,
    ) -> ModelSelection:
        ...


class ApprovalStore(Protocol):
    """审批记录持久化协议。"""

    async def save_approval(self, approval: ApprovalRecord) -> None:
        ...

    async def load_approval(self, approval_id: str) -> ApprovalRecord | None:
        ...


class RunStore(Protocol):
    """运行实例持久化协议。

    实现应按 ``RunContext`` 中的 tenant/user/thread 作用域隔离数据，并在保存时
    处理版本冲突和幂等键。Core 只要求最小的读取、保存和恢复能力。
    """

    async def save_run(self, context: RunContext) -> None:
        """保存运行上下文。实现可以在这里递增并回写 ``context.version``。"""
        ...

    async def load_run(self, thread_id: str, run_id: str) -> RunContext | None:
        """按会话和运行 ID读取运行上下文。"""
        ...

    async def find_run_by_idempotency(
        self,
        tenant_id: str | None,
        user_id: str | None,
        idempotency_key: str,
    ) -> RunContext | None:
        """查找同一作用域内已经存在的幂等请求。"""
        ...


class SessionStore(Protocol):
    """对话消息历史存储协议。"""

    async def load_messages(self, thread_id: str) -> list[Message]:
        ...

    async def append_messages(self, thread_id: str, messages: Sequence[Message]) -> None:
        ...

    async def replace_messages(self, thread_id: str, messages: Sequence[Message]) -> None:
        ...

    async def delete_thread(self, thread_id: str) -> None:
        ...

    async def list_threads(self) -> list[str]:
        ...


class ContextStore(Protocol):
    """结构化长期上下文存储协议。"""

    async def get_context(self, thread_id: str) -> dict[str, Any]:
        ...

    async def update_context(self, thread_id: str, values: dict[str, Any]) -> dict[str, Any]:
        ...

    async def clear_context(self, thread_id: str) -> None:
        ...


class WorkflowStore(Protocol):
    """工作流执行实例存储协议。"""

    async def save_execution(self, execution: Any) -> None:
        ...

    async def load_execution(self, execution_id: str) -> Any | None:
        ...


class EventSink(Protocol):
    """运行事件发布协议，可接 SSE、WebSocket、日志或消息队列。"""

    async def publish(self, event: RunEvent) -> None:
        ...
