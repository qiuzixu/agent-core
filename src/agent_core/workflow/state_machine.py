"""手写状态机调度器。

核心概念：
- State: 字典，存储所有运行时状态
- Node: 可调用函数，接受 state，返回更新字典
- Edge: 节点间的转移规则（固定边 / 条件边）
- Execution: 从 START 开始，按边的规则依次执行节点，直到 END
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
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
        # 固定边：node_name -> next_node_name
        self._edges: dict[str, str] = {}
        # 条件边：node_name -> route_func
        self._conditional_edges: dict[str, ConditionalRouteFunc] = {}
        self._max_steps = max_steps

    def add_node(self, name: str, func: NodeFunc) -> None:
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
    ) -> None:
        """注册一条条件边：from_node 执行完毕后，调用 route_func(state) 决定下一个节点。

        Args:
            from_node: 源节点名称。
            route_func: 路由函数，接受 state，返回下一个节点名称。
        """
        if from_node in self._edges:
            raise StateMachineError(f"Node {from_node!r} already has a fixed edge.")
        self._conditional_edges[from_node] = route_func

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
                    updates = node_func(state)
                    if isawaitable(updates):
                        updates = await updates
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

    def add_node(self, name: str, func: NodeFunc) -> StateMachineBuilder:
        """添加节点（链式调用）。"""
        self._machine.add_node(name, func)
        return self

    def add_edge(self, from_node: str, to_node: str) -> StateMachineBuilder:
        """添加固定边（链式调用）。"""
        self._machine.add_edge(from_node, to_node)
        return self

    def add_conditional_edges(
        self,
        from_node: str,
        route_func: ConditionalRouteFunc,
    ) -> StateMachineBuilder:
        """添加条件边（链式调用）。"""
        self._machine.add_conditional_edges(from_node, route_func)
        return self

    def build(self) -> StateMachine:
        """构建并返回状态机实例。"""
        return self._machine
