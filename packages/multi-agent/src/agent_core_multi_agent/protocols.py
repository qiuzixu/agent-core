"""多 Agent 扩展的依赖倒置协议。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agent_core import AccessContext

from agent_core_multi_agent.types import AgentResult, AgentTask, CoordinationExecution, RoutingDecision

if TYPE_CHECKING:
    from agent_core_multi_agent.registry import AgentRegistry


@runtime_checkable
class AgentInvoker(Protocol):
    """任何可由 Supervisor 调用的 Agent 最小接口。"""

    async def invoke(self, task: AgentTask) -> AgentResult: ...


@runtime_checkable
class AgentRouter(Protocol):
    """根据任务和当前可访问注册表选择 Agent。"""

    async def route(self, task: AgentTask, registry: AgentRegistry) -> RoutingDecision: ...


@runtime_checkable
class CoordinationStore(Protocol):
    """协调实例持久化协议。"""

    async def save(self, execution: CoordinationExecution) -> None: ...

    async def load(
        self,
        execution_id: str,
        *,
        access: AccessContext | None = None,
    ) -> CoordinationExecution | None: ...

    async def list(
        self,
        *,
        access: AccessContext | None = None,
    ) -> list[CoordinationExecution]: ...


__all__ = ["AgentInvoker", "AgentRouter", "CoordinationStore"]
