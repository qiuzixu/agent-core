"""手写状态机调度器。

核心概念：
- State: 字典，存储所有运行时状态
- Node: 可调用函数，接受 state，返回更新字典
- Edge: 节点间的转移规则（固定边 / 条件边）
- Execution: 从 START 开始，按边的规则依次执行节点，直到 END
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

from agent_core.errors import StateMachineError

logger = logging.getLogger(__name__)

# 特殊节点名称
START = "__start__"
END = "__end__"

# 节点函数签名
NodeFunc = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]
# 条件路由函数签名：返回下一个节点名称
ConditionalRouteFunc = Callable[[dict[str, Any]], str | Awaitable[str]]
StepCallback = Callable[
    [str, str, dict[str, Any], int],
    Awaitable[None] | None,
]
NodeStartCallback = Callable[["NodeExecutionContext", dict[str, Any]], Awaitable[None] | None]
IdempotencyKeyFactory = Callable[[str, int], str]


@dataclass(frozen=True)
class NodeExecutionPolicy:
    """单个工作流节点的超时和重试策略。"""

    timeout_seconds: float | None = None
    max_attempts: int = 1
    backoff_initial_seconds: float = 0.0
    backoff_multiplier: float = 2.0
    backoff_max_seconds: float = 30.0
    retry_exceptions: tuple[type[Exception], ...] = (Exception,)
    idempotent: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts 必须大于 0")
        if self.max_attempts > 1 and not self.idempotent:
            raise ValueError("开启节点重试前必须声明 idempotent=True")
        if self.backoff_initial_seconds < 0 or self.backoff_max_seconds < 0:
            raise ValueError("退避时间不能小于 0")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier 不能小于 1")


@dataclass(frozen=True)
class NodeExecutionContext:
    """当前节点调用可读取的执行上下文。"""

    node_name: str
    attempt: int
    idempotency_key: str


_CURRENT_NODE_EXECUTION: ContextVar[NodeExecutionContext | None] = ContextVar(
    "agent_core_node_execution",
    default=None,
)


def current_node_execution() -> NodeExecutionContext | None:
    """返回当前节点执行上下文，供外部副作用传递幂等键。"""

    return _CURRENT_NODE_EXECUTION.get()


class WorkflowPause(Exception):
    """节点主动暂停工作流，等待外部条件满足后恢复。"""

    def __init__(self, reason: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.data = dict(data or {})


class StateMachine:
    """手写状态机调度器。

    功能：
    1. 注册节点（node）和边（edge）
    2. 从 START 开始执行，按照边的规则转移
    3. 每执行一个节点，将返回值合并到 state
    4. 条件边根据当前 state 动态决定下一个节点
    5. 到达 END 或超过最大步数时停止
    """

    def __init__(self, *, max_steps: int = 50) -> None:
        """
        Args:
            max_steps: 最大执行步数，防止死循环。
        """
        self._nodes: dict[str, NodeFunc] = {}
        self._node_policies: dict[str, NodeExecutionPolicy] = {}
        # 固定边：node_name -> next_node_name
        self._edges: dict[str, str] = {}
        # 条件边：node_name -> route_func
        self._conditional_edges: dict[str, ConditionalRouteFunc] = {}
        self._conditional_targets: dict[str, tuple[str, ...]] = {}
        self._max_steps = max_steps

    def add_node(
        self,
        name: str,
        func: NodeFunc,
        *,
        policy: NodeExecutionPolicy | None = None,
    ) -> None:
        """注册一个节点。

        Args:
            name: 节点名称（全局唯一）。
            func: 节点函数，接受 state，返回更新字典。
        """
        if name in self._nodes:
            raise StateMachineError(f"Node {name!r} already registered.")
        if name in (START, END):
            raise StateMachineError(f"Cannot register reserved node name {name!r}.")
        self._nodes[name] = func
        self._node_policies[name] = policy or NodeExecutionPolicy()

    def add_edge(self, from_node: str, to_node: str) -> None:
        """注册一条固定边：from_node 执行完毕后自动转移到 to_node。

        Args:
            from_node: 源节点名称。
            to_node: 目标节点名称。
        """
        if from_node in self._conditional_edges:
            raise StateMachineError(f"Node {from_node!r} already has a conditional edge.")
        self._edges[from_node] = to_node

    def add_conditional_edges(
        self,
        from_node: str,
        route_func: ConditionalRouteFunc,
        *,
        possible_targets: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """注册一条条件边：from_node 执行完毕后，调用 route_func(state) 决定下一个节点。

        Args:
            from_node: 源节点名称。
            route_func: 路由函数，接受 state，返回下一个节点名称。
        """
        if from_node in self._edges:
            raise StateMachineError(f"Node {from_node!r} already has a fixed edge.")
        self._conditional_edges[from_node] = route_func
        self._conditional_targets[from_node] = tuple(possible_targets or ())

    async def ainvoke(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        """异步执行状态机。

        Args:
            initial_state: 初始状态字典。

        Returns:
            执行完毕后的最终状态。

        Raises:
            StateMachineError: 节点不存在、超过最大步数等错误。
        """
        return await self.ainvoke_from(initial_state, start_node=START)

    async def ainvoke_from(
        self,
        initial_state: dict[str, Any],
        *,
        start_node: str = START,
        on_step: StepCallback | None = None,
        on_node_start: NodeStartCallback | None = None,
        idempotency_key_factory: IdempotencyKeyFactory | None = None,
    ) -> dict[str, Any]:
        """从指定节点执行，并在每个节点完成后发布可持久化快照。"""
        if start_node != START and start_node != END and start_node not in self._nodes:
            raise StateMachineError(f"Node {start_node!r} not found.")
        state = deepcopy(initial_state)
        current_node = start_node
        steps = 0
        completed_nodes = 0

        logger.info("StateMachine: starting execution from %s", current_node)

        while current_node != END:
            steps += 1
            if steps > self._max_steps:
                raise StateMachineError(
                    f"StateMachine exceeded max steps ({self._max_steps}). Last node: {current_node!r}."
                )

            # 执行当前节点（START 和 END 是虚拟节点，不执行）
            if current_node != START:
                node_func = self._nodes.get(current_node)
                if node_func is None:
                    raise StateMachineError(f"Node {current_node!r} not found.")

                logger.debug("StateMachine: executing node %r (step %d)", current_node, steps)
                try:
                    idempotency_key = (
                        idempotency_key_factory(current_node, completed_nodes)
                        if idempotency_key_factory is not None
                        else f"{current_node}:{completed_nodes + 1}"
                    )
                    updates = await self._execute_node(
                        current_node,
                        node_func,
                        state,
                        idempotency_key=idempotency_key,
                        on_node_start=on_node_start,
                    )
                    if updates:
                        state.update(updates)
                    completed_nodes += 1
                except WorkflowPause:
                    raise
                except Exception as exc:
                    raise StateMachineError(f"Node {current_node!r} raised exception: {exc}") from exc

            # 决定下一个节点
            next_node = await self._get_next_node(current_node, state)
            if on_step is not None:
                checkpoint = on_step(current_node, next_node, deepcopy(state), completed_nodes)
                if isawaitable(checkpoint):
                    await checkpoint
            logger.debug("StateMachine: %r -> %r", current_node, next_node)
            current_node = next_node

        logger.info("StateMachine: execution completed in %d steps", steps)
        return state

    @property
    def node_count(self) -> int:
        """返回已注册的实际节点数量。"""
        return len(self._nodes)

    async def _execute_node(
        self,
        node_name: str,
        node_func: NodeFunc,
        state: dict[str, Any],
        *,
        idempotency_key: str,
        on_node_start: NodeStartCallback | None,
    ) -> dict[str, Any]:
        policy = self._node_policies[node_name]
        delay = policy.backoff_initial_seconds
        for attempt in range(1, policy.max_attempts + 1):
            context = NodeExecutionContext(
                node_name=node_name,
                attempt=attempt,
                idempotency_key=idempotency_key,
            )
            if on_node_start is not None:
                pending = on_node_start(context, deepcopy(state))
                if isawaitable(pending):
                    await pending
            token = _CURRENT_NODE_EXECUTION.set(context)
            try:
                result = node_func(state)
                if isawaitable(result):
                    if policy.timeout_seconds is None:
                        result = await result
                    else:
                        result = await asyncio.wait_for(result, timeout=policy.timeout_seconds)
                return result
            except WorkflowPause:
                raise
            except policy.retry_exceptions:
                if attempt >= policy.max_attempts:
                    raise
                if delay > 0:
                    await asyncio.sleep(delay)
                    delay = min(
                        policy.backoff_max_seconds,
                        max(delay, 0.001) * policy.backoff_multiplier,
                    )
            finally:
                _CURRENT_NODE_EXECUTION.reset(token)
        raise StateMachineError(f"Node {node_name!r} exhausted retry policy.")

    def describe(self) -> dict[str, Any]:
        """返回可序列化的状态机结构，用于调试和可视化。"""

        return {
            "nodes": [
                {
                    "name": name,
                    "policy": {
                        "timeout_seconds": policy.timeout_seconds,
                        "max_attempts": policy.max_attempts,
                        "idempotent": policy.idempotent,
                    },
                }
                for name, policy in self._node_policies.items()
            ],
            "edges": [
                {"from": source, "to": target, "kind": "fixed"} for source, target in self._edges.items()
            ],
            "conditional_edges": [
                {
                    "from": source,
                    "targets": list(self._conditional_targets.get(source, ())),
                    "kind": "conditional",
                }
                for source in self._conditional_edges
            ],
            "max_steps": self._max_steps,
        }

    def to_mermaid(self, *, direction: str = "TD") -> str:
        """把状态机导出为 Mermaid flowchart。"""

        normalized_direction = direction.upper()
        if normalized_direction not in {"TD", "TB", "BT", "LR", "RL"}:
            raise ValueError(f"不支持的 Mermaid 方向：{direction}")
        names = [START, *self._nodes, END]
        node_ids = {name: f"N{index}" for index, name in enumerate(names)}

        def label(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', "&quot;")

        lines = [f"flowchart {normalized_direction}"]
        lines.append(f'    {node_ids[START]}(["START"])')
        for name in self._nodes:
            lines.append(f'    {node_ids[name]}["{label(name)}"]')
        lines.append(f'    {node_ids[END]}(["END"])')
        for source, target in self._edges.items():
            if source in node_ids and target in node_ids:
                lines.append(f"    {node_ids[source]} --> {node_ids[target]}")
        for index, source in enumerate(self._conditional_edges):
            targets = self._conditional_targets.get(source, ())
            if targets:
                for target in targets:
                    if target in node_ids:
                        lines.append(f'    {node_ids[source]} -. "条件" .-> {node_ids[target]}')
            else:
                dynamic_id = f"D{index}"
                lines.append(f'    {dynamic_id}{{"动态路由"}}')
                lines.append(f'    {node_ids[source]} -. "条件" .-> {dynamic_id}')
        return "\n".join(lines)

    async def _get_next_node(self, current_node: str, state: dict[str, Any]) -> str:
        """根据边的规则决定下一个节点。

        优先级：
        1. 条件边（如果存在）
        2. 固定边（如果存在）
        3. 默认：END（终止执行）
        """
        # 条件边
        if current_node in self._conditional_edges:
            route_func = self._conditional_edges[current_node]
            try:
                next_node = route_func(state)
                return await next_node if isawaitable(next_node) else next_node
            except Exception as exc:
                raise StateMachineError(
                    f"Conditional route from {current_node!r} raised exception: {exc}"
                ) from exc

        # 固定边
        if current_node in self._edges:
            return self._edges[current_node]

        # 默认终止
        return END


class StateMachineBuilder:
    """状态机构建器（类似 StateGraph 的接口风格）。"""

    def __init__(self, *, max_steps: int = 50) -> None:
        self._machine = StateMachine(max_steps=max_steps)

    def add_node(
        self,
        name: str,
        func: NodeFunc,
        *,
        policy: NodeExecutionPolicy | None = None,
    ) -> StateMachineBuilder:
        """添加节点（链式调用）。"""
        self._machine.add_node(name, func, policy=policy)
        return self

    def add_edge(self, from_node: str, to_node: str) -> StateMachineBuilder:
        """添加固定边（链式调用）。"""
        self._machine.add_edge(from_node, to_node)
        return self

    def add_conditional_edges(
        self,
        from_node: str,
        route_func: ConditionalRouteFunc,
        *,
        possible_targets: list[str] | tuple[str, ...] | None = None,
    ) -> StateMachineBuilder:
        """添加条件边（链式调用）。"""
        self._machine.add_conditional_edges(
            from_node,
            route_func,
            possible_targets=possible_targets,
        )
        return self

    def build(self) -> StateMachine:
        """构建并返回状态机实例。"""
        return self._machine
