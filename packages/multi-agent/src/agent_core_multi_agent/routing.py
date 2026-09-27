"""确定性能力路由和可选模型路由。"""

from __future__ import annotations

import json
from typing import Any

from agent_core import ModelAdapter, StructuredOutputSpec, chat_structured, system_message, user_message

from agent_core_multi_agent.errors import AgentRoutingError
from agent_core_multi_agent.registry import AgentRegistry
from agent_core_multi_agent.types import AgentTask, RoutingDecision


class CapabilityRouter:
    """优先显式目标，其次能力匹配，最后使用可选默认 Agent。"""

    def __init__(self, default_agent_id: str | None = None) -> None:
        self._default_agent_id = default_agent_id

    async def route(self, task: AgentTask, registry: AgentRegistry) -> RoutingDecision:
        if task.target_agent_id:
            descriptor = registry.get_descriptor(task.target_agent_id, access=task.access)
            if (
                task.required_capability
                and task.required_capability not in descriptor.capabilities
            ):
                raise AgentRoutingError(
                    f"Agent {descriptor.agent_id} 不提供能力：{task.required_capability}"
                )
            return RoutingDecision(descriptor.agent_id, "任务显式指定目标 Agent")

        if task.required_capability:
            candidates = registry.candidates(task.required_capability, access=task.access)
            if not candidates:
                raise AgentRoutingError(f"没有可访问的 Agent 提供能力：{task.required_capability}")
            selected = candidates[0]
            return RoutingDecision(
                selected.agent_id,
                f"按能力 {task.required_capability} 和优先级选择",
            )

        if self._default_agent_id:
            descriptor = registry.get_descriptor(self._default_agent_id, access=task.access)
            return RoutingDecision(descriptor.agent_id, "使用默认 Agent")

        candidates = registry.descriptors(access=task.access)
        if len(candidates) == 1:
            return RoutingDecision(candidates[0].agent_id, "只有一个可访问的 Agent")
        raise AgentRoutingError("任务没有指定目标或能力，且无法唯一选择 Agent")


class ModelAgentRouter:
    """让模型在权限过滤后的候选集合中选择 Agent。"""

    def __init__(
        self,
        model: ModelAdapter,
        *,
        fallback: CapabilityRouter | None = None,
        max_retries: int = 1,
    ) -> None:
        self._model = model
        self._fallback = fallback or CapabilityRouter()
        self._max_retries = max_retries

    async def route(self, task: AgentTask, registry: AgentRegistry) -> RoutingDecision:
        if task.target_agent_id or task.required_capability:
            return await self._fallback.route(task, registry)

        candidates = registry.descriptors(access=task.access)
        if not candidates:
            raise AgentRoutingError("没有可访问的 Agent")
        if len(candidates) == 1:
            return RoutingDecision(candidates[0].agent_id, "只有一个可访问的 Agent")

        agent_ids = [item.agent_id for item in candidates]
        candidate_payload = [
            {
                "agent_id": item.agent_id,
                "description": item.description,
                "capabilities": sorted(item.capabilities),
            }
            for item in candidates
        ]
        spec = StructuredOutputSpec[RoutingDecision](
            name="agent_routing_decision",
            schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "enum": agent_ids},
                    "reason": {"type": "string", "minLength": 1},
                },
                "required": ["agent_id", "reason"],
                "additionalProperties": False,
            },
            decoder=self._decode,
            max_retries=self._max_retries,
        )
        result = await chat_structured(
            self._model,
            [
                system_message(
                    "你是多 Agent 路由器。只能从候选 Agent 中选择一个最适合当前任务的 Agent。"
                ),
                user_message(
                    "任务："
                    f"{task.input}\n上下文：{json.dumps(task.context, ensure_ascii=False, default=str)}"
                    f"\n候选：{json.dumps(candidate_payload, ensure_ascii=False)}"
                ),
            ],
            spec,
            temperature=0,
        )
        return result.value

    @staticmethod
    def _decode(value: Any) -> RoutingDecision:
        if not isinstance(value, dict):
            raise TypeError("路由结果必须是对象")
        return RoutingDecision(agent_id=str(value["agent_id"]), reason=str(value["reason"]))


__all__ = ["CapabilityRouter", "ModelAgentRouter"]
