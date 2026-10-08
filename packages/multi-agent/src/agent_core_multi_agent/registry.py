"""Agent 注册、能力发现、权限过滤和并发限制。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent_core import AccessContext

from agent_core_multi_agent.errors import (
    AgentAccessDeniedError,
    AgentInvocationError,
    AgentNotFoundError,
    AgentRegistrationError,
)
from agent_core_multi_agent.protocols import AgentInvoker
from agent_core_multi_agent.types import AgentDescriptor, AgentResult, AgentTask


@dataclass(frozen=True)
class _RegisteredAgent:
    descriptor: AgentDescriptor
    invoker: AgentInvoker
    semaphore: asyncio.Semaphore | None


class AgentRegistry:
    """保存 Agent 描述和调用器，并在调用前执行权限与并发检查。"""

    def __init__(self) -> None:
        self._agents: dict[str, _RegisteredAgent] = {}
        self._aliases: dict[str, str] = {}

    def register(
        self,
        descriptor: AgentDescriptor,
        invoker: AgentInvoker,
        *,
        replace: bool = False,
    ) -> None:
        agent_id = descriptor.agent_id
        collisions = {
            name
            for name in (agent_id, *descriptor.aliases)
            if name in self._aliases and self._aliases[name] != agent_id
        }
        if collisions:
            raise AgentRegistrationError(f"Agent 名称或别名冲突：{', '.join(sorted(collisions))}")
        if agent_id in self._agents and not replace:
            raise AgentRegistrationError(f"Agent 已注册：{agent_id}")
        if replace:
            self.unregister(agent_id)
        semaphore = (
            asyncio.Semaphore(descriptor.max_concurrency) if descriptor.max_concurrency is not None else None
        )
        self._agents[agent_id] = _RegisteredAgent(descriptor, invoker, semaphore)
        self._aliases[agent_id] = agent_id
        for alias in descriptor.aliases:
            self._aliases[alias] = agent_id

    def unregister(self, agent_id: str) -> bool:
        normalized = self._aliases.get(agent_id, agent_id)
        registered = self._agents.pop(normalized, None)
        if registered is None:
            return False
        for name, owner in tuple(self._aliases.items()):
            if owner == normalized:
                del self._aliases[name]
        return True

    def get_descriptor(
        self,
        agent_id: str,
        *,
        access: AccessContext | None = None,
    ) -> AgentDescriptor:
        registered = self._resolve(agent_id)
        self._check_access(registered.descriptor, access)
        return registered.descriptor

    def descriptors(self, *, access: AccessContext | None = None) -> tuple[AgentDescriptor, ...]:
        values = [item.descriptor for item in self._agents.values() if item.descriptor.can_invoke(access)]
        return tuple(sorted(values, key=lambda item: (-item.priority, item.agent_id)))

    def candidates(
        self,
        capability: str,
        *,
        access: AccessContext | None = None,
    ) -> tuple[AgentDescriptor, ...]:
        normalized = capability.strip()
        return tuple(
            descriptor
            for descriptor in self.descriptors(access=access)
            if normalized in descriptor.capabilities
        )

    async def invoke(self, agent_id: str, task: AgentTask) -> AgentResult:
        registered = self._resolve(agent_id)
        self._check_access(registered.descriptor, task.access)

        async def execute() -> AgentResult:
            result = await registered.invoker.invoke(task)
            if result.task_id != task.task_id:
                raise AgentInvocationError(
                    f"Agent {registered.descriptor.agent_id} 返回了错误的 task_id：{result.task_id}"
                )
            if result.agent_id != registered.descriptor.agent_id:
                raise AgentInvocationError(
                    f"Agent 返回 ID {result.agent_id} 与注册 ID {registered.descriptor.agent_id} 不一致"
                )
            return result

        if registered.semaphore is None:
            return await execute()
        async with registered.semaphore:
            return await execute()

    def _resolve(self, name: str) -> _RegisteredAgent:
        agent_id = self._aliases.get(name)
        if agent_id is None:
            available = ", ".join(sorted(self._agents)) or "无"
            raise AgentNotFoundError(f"Agent 不存在：{name}；已注册：{available}")
        return self._agents[agent_id]

    @staticmethod
    def _check_access(descriptor: AgentDescriptor, access: AccessContext | None) -> None:
        if not descriptor.can_invoke(access):
            roles = ", ".join(sorted(descriptor.required_roles))
            raise AgentAccessDeniedError(f"调用 Agent {descriptor.agent_id} 需要角色：{roles}")


__all__ = ["AgentRegistry"]
