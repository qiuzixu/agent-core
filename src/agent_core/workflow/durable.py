"""状态机与工作流执行存储的持久化运行器。"""

from __future__ import annotations

from typing import Any

from agent_core.access import AccessContext
from agent_core.storage.workflow import WorkflowExecution, WorkflowExecutionStore
from agent_core.workflow.state_machine import END, START, StateMachine, WorkflowPause


class DurableWorkflowRunner:
    """在每个节点边界保存状态，并支持进程重启后继续执行。"""

    def __init__(
        self,
        machine: StateMachine,
        store: WorkflowExecutionStore,
        *,
        require_access: bool = False,
    ) -> None:
        self._machine = machine
        self._store = store
        self._strict_access = require_access

    async def start(
        self,
        definition_id: str,
        initial_state: dict[str, Any],
        *,
        execution_type: str = "workflow",
        access: AccessContext | None = None,
    ) -> WorkflowExecution:
        """创建执行实例并运行到完成、暂停或失败。"""
        if self._strict_access and access is None:
            raise PermissionError("该 DurableWorkflowRunner 要求提供 AccessContext")
        execution = WorkflowExecution(
            definition_id=definition_id,
            execution_type=execution_type,
            user_id=access.user_id if access else None,
            tenant_id=access.tenant_id if access else None,
            status="queued",
            current_step=START,
            total_steps=self._machine.node_count,
            input_data=dict(initial_state),
            result_data=dict(initial_state),
            events=[{"event_type": "workflow_queued", "step": START}],
        )
        await self._store.save(execution)
        return await self._run(execution)

    async def resume(
        self,
        execution_id: str,
        *,
        access: AccessContext | None = None,
    ) -> WorkflowExecution:
        """从最后保存的下一节点继续执行。"""
        execution = await self._store.load(execution_id)
        if execution is None:
            raise KeyError(f"工作流执行实例不存在：{execution_id}")
        self._check_access(execution, access)
        if execution.status == "completed":
            return execution
        if execution.current_step == END:
            execution.status = "completed"
            await self._store.save(execution)
            return execution
        execution.events.append(
            {"event_type": "workflow_resumed", "step": execution.current_step or START}
        )
        return await self._run(execution)

    async def _run(self, execution: WorkflowExecution) -> WorkflowExecution:
        execution.status = "running"
        await self._store.save(execution)
        completed_offset = execution.steps_completed

        async def persist_step(
            current_node: str,
            next_node: str,
            state: dict[str, Any],
            completed_nodes: int,
        ) -> None:
            execution.current_step = next_node
            execution.steps_completed = completed_offset + completed_nodes
            execution.result_data = state
            execution.events.append(
                {
                    "event_type": "workflow_step_completed",
                    "step": current_node,
                    "next_step": next_node,
                }
            )
            await self._store.save(execution)

        try:
            result = await self._machine.ainvoke_from(
                execution.result_data or execution.input_data,
                start_node=execution.current_step or START,
                on_step=persist_step,
            )
        except WorkflowPause as exc:
            execution.status = "interrupted"
            execution.events.append(
                {
                    "event_type": "workflow_interrupted",
                    "step": execution.current_step,
                    "reason": exc.reason,
                    "data": exc.data,
                }
            )
            await self._store.save(execution)
            return execution
        except Exception as exc:
            execution.status = "failed"
            execution.events.append(
                {
                    "event_type": "workflow_failed",
                    "step": execution.current_step,
                    "error": str(exc),
                }
            )
            await self._store.save(execution)
            raise

        execution.status = "completed"
        execution.current_step = END
        execution.result_data = result
        execution.events.append({"event_type": "workflow_completed", "step": END})
        await self._store.save(execution)
        return execution

    def _check_access(
        self,
        execution: WorkflowExecution,
        access: AccessContext | None,
    ) -> None:
        if self._strict_access and access is None:
            raise PermissionError("该 DurableWorkflowRunner 要求提供 AccessContext")
        if self._strict_access and (
            execution.user_id is None or execution.tenant_id is None
        ):
            raise PermissionError("该工作流执行实例尚未绑定用户和租户")
        if access is not None and not access.can_access(
            execution.user_id,
            execution.tenant_id,
        ):
            raise PermissionError("无权访问该工作流执行实例")


__all__ = ["DurableWorkflowRunner"]
