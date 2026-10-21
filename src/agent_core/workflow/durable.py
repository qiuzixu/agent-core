"""状态机与工作流执行存储的持久化运行器。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agent_core.access import AccessContext, enforce_access
from agent_core.checkpoint.store import Checkpointer, DEFAULT_CHECKPOINT_NS
from agent_core.storage.governance import (
    SessionEvent,
    SessionEventStore,
    WorkflowIdempotencyRecord,
    WorkflowIdempotencyStore,
)
from agent_core.storage.workflow import WorkflowExecution, WorkflowExecutionStore
from agent_core.workflow.state_machine import (
    END,
    START,
    NodeExecutionContext,
    StateMachine,
    WorkflowPause,
)


class WorkflowNodeError(RuntimeError):
    """工作流节点失败；retryable=True 时外层可进入 waiting_retry。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _is_retryable_error(exc: BaseException) -> bool:
    """按异常类型/文案识别瞬态错误；后续可升级为结构化 error_kind。"""
    message = str(exc).lower()
    return any(
        token in message
        for token in ("timeout", "connection", "temporarily", "rate limit", "超时", "连接", "503", "504")
    )


class DurableWorkflowRunner:
    """在每个节点边界保存状态，并支持进程重启后继续执行。"""

    def __init__(
        self,
        machine: StateMachine,
        store: WorkflowExecutionStore,
        *,
        require_access: bool = False,
        raise_on_failure: bool = True,
        max_retries: int = 2,
        idempotency_store: WorkflowIdempotencyStore | None = None,
        session_event_store: SessionEventStore | None = None,
        checkpointer: Checkpointer | None = None,
    ) -> None:
        self._machine = machine
        self._store = store
        self._strict_access = require_access
        self._raise_on_failure = raise_on_failure
        self._max_retries = max(0, int(max_retries))
        self._idempotency_store = idempotency_store
        self._session_event_store = session_event_store
        self._checkpointer = checkpointer

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
        # 将 execution ID 注入状态，供内层 ReAct / open_qa 建立父子运行谱系。
        initial_state = {**initial_state, "workflow_execution_id": execution.execution_id}
        execution.input_data = dict(initial_state)
        execution.result_data = dict(initial_state)
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
        # 尝试从共享 checkpointer 的 workflow namespace 加载最新状态。
        # checkpoint 与 execution_store.result_data 应该一致；如果 checkpoint 更新
        # （例如 store 写入失败但 checkpoint 成功），用 checkpoint 的状态恢复。
        if self._checkpointer is not None:
            state = dict(execution.result_data or {})
            thread_id = str(
                state.get("source_thread_id")
                or state.get("thread_id")
                or execution.execution_id
            )
            ns = f"workflow:{execution.execution_id}"
            try:
                checkpoint_state = await self._checkpointer.load(thread_id, checkpoint_ns=ns)
            except Exception:
                checkpoint_state = None
            if checkpoint_state is not None:
                execution.result_data = checkpoint_state
        await self._emit(
            execution,
            {"event_type": "workflow_resumed", "step": execution.current_step or START},
        )
        return await self._run(execution)

    async def _emit(self, execution: WorkflowExecution, event: dict[str, Any]) -> None:
        """追加执行事件，并同步到统一会话事件 feed。"""
        execution.events.append(event)
        store = self._session_event_store
        if store is None:
            return
        state = dict(execution.result_data or {})
        thread_id = str(
            state.get("source_thread_id")
            or state.get("thread_id")
            or execution.execution_id
        )
        await store.append(
            SessionEvent(
                thread_id=thread_id,
                run_type="workflow",
                run_id=execution.execution_id,
                event_type=str(event.get("event_type", "workflow_event")),
                payload={
                    **{key: value for key, value in event.items() if key != "event_type"},
                    "workflow_id": execution.definition_id,
                    "status": execution.status,
                },
            )
        )

    async def _run(self, execution: WorkflowExecution) -> WorkflowExecution:
        execution.status = "running"
        await self._store.save(execution)
        completed_offset = execution.steps_completed

        def idempotency_key(node_name: str, completed_nodes: int) -> str:
            sequence = completed_offset + completed_nodes + 1
            return f"{execution.execution_id}:{sequence}:{node_name}"

        async def persist_pending(context: NodeExecutionContext, _state: dict[str, Any]) -> None:
            idempotency_store = self._idempotency_store
            if idempotency_store is not None:
                await idempotency_store.reserve(
                    WorkflowIdempotencyRecord(
                        key=context.idempotency_key,
                        workflow_id=execution.definition_id,
                        execution_id=execution.execution_id,
                        node_id=context.node_name,
                        arguments_hash=str(context.idempotency_key),
                    )
                )
            await self._emit(
                execution,
                {
                    "event_type": "workflow_step_pending",
                    "step": context.node_name,
                    "attempt": context.attempt,
                    "idempotency_key": context.idempotency_key,
                },
            )

        async def persist_step(
            current_node: str,
            next_node: str,
            state: dict[str, Any],
            completed_nodes: int,
        ) -> None:
            execution.current_step = next_node
            execution.steps_completed = completed_offset + completed_nodes
            execution.result_data = state
            idempotency_store = self._idempotency_store
            if idempotency_store is not None:
                await idempotency_store.complete(
                    idempotency_key(current_node, completed_nodes - 1),
                    result={"result_data": state},
                    message="workflow node completed",
                )
            await self._save_checkpoint(execution)
            await self._emit(
                execution,
                {
                    "event_type": "workflow_step_completed",
                    "step": current_node,
                    "next_step": next_node,
                    "idempotency_key": idempotency_key(current_node, completed_nodes - 1),
                },
            )

        try:
            result = await self._machine.ainvoke_from(
                execution.result_data,
                start_node=execution.current_step or START,
                on_step=persist_step,
                on_node_start=persist_pending,
                idempotency_key_factory=idempotency_key,
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
            retryable = bool(getattr(exc, "retryable", False)) or _is_retryable_error(exc)
            retry_count = int((execution.result_data or {}).get("workflow_retry_count") or 0)
            if retryable and retry_count < self._max_retries:
                delay = 1.0 * (2**retry_count)
                next_retry_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
                state = dict(execution.result_data or {})
                state["workflow_retry_count"] = retry_count + 1
                state["workflow_pending_retry"] = {
                    "node_id": execution.current_step,
                    "retry_count": retry_count + 1,
                    "retry_after_seconds": delay,
                    "next_retry_at": next_retry_at,
                    "error": str(exc),
                }
                execution.status = "waiting_retry"
                execution.result_data = state
                await self._emit(
                    execution,
                    {
                        "event_type": "workflow_node_waiting_retry",
                        "step": execution.current_step,
                        "retry_count": retry_count + 1,
                        "next_retry_at": next_retry_at,
                        "error": str(exc),
                    },
                )
                await self._store.save(execution)
                return execution

            execution.status = "failed"
            await self._emit(
                execution,
                {
                    "event_type": "workflow_failed",
                    "step": execution.current_step,
                    "error": str(exc),
                },
            )
            await self._store.save(execution)
            if self._raise_on_failure:
                raise
            return execution

        execution.status = "completed"
        execution.current_step = END
        execution.result_data = result
        await self._emit(execution, {"event_type": "workflow_completed", "step": END})
        await self._store.save(execution)
        return execution

    async def _save_checkpoint(self, execution: WorkflowExecution) -> None:
        """把工作流当前状态存到共享 checkpointer 的 workflow namespace 下。"""
        if self._checkpointer is None:
            return
        state = dict(execution.result_data or {})
        thread_id = str(
            state.get("source_thread_id")
            or state.get("thread_id")
            or execution.execution_id
        )
        ns = f"workflow:{execution.execution_id}"
        await self._checkpointer.save(thread_id, state, checkpoint_ns=ns)

    def _check_access(
        self,
        execution: WorkflowExecution,
        access: AccessContext | None,
    ) -> None:
        enforce_access(
            strict=self._strict_access,
            accessor="DurableWorkflowRunner",
            subject="工作流执行实例",
            owner_user_id=execution.user_id,
            owner_tenant_id=execution.tenant_id,
            access=access,
        )


__all__ = ["DurableWorkflowRunner", "WorkflowNodeError"]
