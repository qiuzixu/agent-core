"""复用 Core Workflow Store 的协调实例存储适配器。"""

from __future__ import annotations

import json

from agent_core import (
    AccessContext,
    MemoryWorkflowExecutionStore,
    WorkflowExecution,
    WorkflowExecutionStore,
)

from agent_core_multi_agent.types import CoordinationExecution


class WorkflowCoordinationStore:
    """把协调快照保存到 Core 的 Memory/SQLite/PostgreSQL Workflow Store。"""

    def __init__(
        self,
        store: WorkflowExecutionStore | None = None,
        *,
        definition_id: str = "multi-agent-supervisor",
        require_access: bool = False,
    ) -> None:
        self._store = store or MemoryWorkflowExecutionStore()
        self._definition_id = definition_id
        self._require_access = require_access

    async def save(self, execution: CoordinationExecution) -> None:
        access = execution.access
        if self._require_access and access is None:
            raise PermissionError("该 CoordinationStore 要求提供 AccessContext")
        payload = execution.to_dict()
        try:
            json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("协调实例包含不可 JSON 序列化的数据") from exc
        record = WorkflowExecution(
            execution_id=execution.execution_id,
            definition_id=self._definition_id,
            execution_type="multi_agent",
            user_id=access.user_id if access else None,
            tenant_id=access.tenant_id if access else None,
            status=execution.status,
            current_step=execution.current_agent_id,
            steps_completed=len(execution.results),
            total_steps=execution.agent_calls,
            input_data=execution.root_task.to_dict(),
            result_data=payload,
            events=[event.to_dict() for event in execution.events],
            version=execution.version,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
        )
        await self._store.save(record)
        execution.version = record.version
        execution.updated_at = record.updated_at

    async def load(
        self,
        execution_id: str,
        *,
        access: AccessContext | None = None,
    ) -> CoordinationExecution | None:
        record = await self._store.load(execution_id)
        if record is None or record.execution_type != "multi_agent":
            return None
        self._check_access(record, access)
        execution = CoordinationExecution.from_dict(record.result_data)
        execution.version = record.version
        execution.updated_at = record.updated_at
        return execution

    async def list(
        self,
        *,
        access: AccessContext | None = None,
    ) -> list[CoordinationExecution]:
        if self._require_access and access is None:
            raise PermissionError("该 CoordinationStore 要求提供 AccessContext")
        tenant_id = access.tenant_id if access else None
        records = await self._store.list(self._definition_id, tenant_id)
        executions: list[CoordinationExecution] = []
        for record in records:
            if record.execution_type != "multi_agent":
                continue
            if access is not None and not access.can_access(record.user_id, record.tenant_id):
                continue
            execution = CoordinationExecution.from_dict(record.result_data)
            execution.version = record.version
            execution.updated_at = record.updated_at
            executions.append(execution)
        return executions

    def _check_access(
        self,
        record: WorkflowExecution,
        access: AccessContext | None,
    ) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 CoordinationStore 要求提供 AccessContext")
        if self._require_access and (record.user_id is None or record.tenant_id is None):
            raise PermissionError("该协调实例尚未绑定用户和租户")
        if access is not None and not access.can_access(record.user_id, record.tenant_id):
            raise PermissionError("无权访问该协调实例")


__all__ = ["WorkflowCoordinationStore"]
