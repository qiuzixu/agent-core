"""函数和 Core AgentRuntime 的调用适配器。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from agent_core import AgentRuntime, RunContext

from agent_core_multi_agent.types import AgentResult, AgentResultStatus, AgentTask

AgentFunction = Callable[
    [AgentTask],
    AgentResult | str | Awaitable[AgentResult | str],
]
RuntimeResultMapper = Callable[
    [RunContext, AgentTask],
    AgentResult | Awaitable[AgentResult],
]


class CallableAgentInvoker:
    """把一个异步函数包装成 AgentInvoker。"""

    def __init__(self, agent_id: str, function: AgentFunction) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id 不能为空")
        self._agent_id = agent_id
        self._function = function

    async def invoke(self, task: AgentTask) -> AgentResult:
        called = self._function(task)
        value = await called if inspect.isawaitable(called) else called
        if isinstance(value, AgentResult):
            return value
        return AgentResult(
            task_id=task.task_id,
            agent_id=self._agent_id,
            status="completed",
            output=str(value),
        )


class RuntimeAgentInvoker:
    """把现有 ``AgentRuntime`` 接入多 Agent 注册表。"""

    def __init__(
        self,
        agent_id: str,
        runtime: AgentRuntime,
        *,
        result_mapper: RuntimeResultMapper | None = None,
    ) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id 不能为空")
        self._agent_id = agent_id
        self._runtime = runtime
        self._result_mapper = result_mapper

    async def invoke(self, task: AgentTask) -> AgentResult:
        access = task.access
        context = await self._runtime.run(
            task.thread_id,
            task.input,
            user_id=access.user_id if access else None,
            tenant_id=access.tenant_id if access else None,
            parent_run_id=task.parent_run_id or task.coordination_id,
            idempotency_key=task.idempotency_key,
            metadata={
                **task.metadata,
                "multi_agent_execution_id": task.coordination_id,
                "multi_agent_task_id": task.task_id,
                "multi_agent_context": task.context,
                "multi_agent_lineage": list(task.lineage),
            },
        )
        if (
            task.resume_requested
            and context.status not in {"completed", "cancelled"}
            and context.checkpoint
        ):
            await self._runtime.resume(
                task.thread_id,
                context.run_id,
                access=access,
            )
            context = await self._runtime.wait(
                task.thread_id,
                context.run_id,
                access=access,
            )
        if self._result_mapper is not None:
            mapped = self._result_mapper(context, task)
            return await mapped if inspect.isawaitable(mapped) else mapped
        return self._default_result(context, task)

    def _default_result(self, context: RunContext, task: AgentTask) -> AgentResult:
        status = _runtime_status(context.status)
        output = ""
        error: str | None = None
        usage: dict[str, float] = {}
        for event in context.events:
            for key, value in event.usage.items():
                if isinstance(value, int | float) and not isinstance(value, bool):
                    usage[key] = usage.get(key, 0.0) + float(value)
            if event.event_type == "run_completed":
                output = str(event.payload.get("answer", ""))
            if event.event_type == "run_failed":
                error = event.error or str(event.payload.get("error", "Agent 运行失败"))
        if status == "failed" and not error:
            error = "Agent 运行失败"
        return AgentResult(
            task_id=task.task_id,
            agent_id=self._agent_id,
            status=status,
            output=output,
            run_id=context.run_id,
            error=error,
            usage=usage,
            metadata={"runtime_status": context.status},
        )


def _runtime_status(status: str) -> AgentResultStatus:
    if status == "completed":
        return "completed"
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    return "interrupted"


__all__ = [
    "AgentFunction",
    "CallableAgentInvoker",
    "RuntimeAgentInvoker",
    "RuntimeResultMapper",
]
