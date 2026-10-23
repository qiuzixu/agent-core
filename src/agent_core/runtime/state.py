"""Agent 运行状态机的唯一收口。

这里集中定义 RunStatus 的合法流转，避免 react/service/vanilla 各自直接赋值
导致出现“completed -> running”这类不该发生的状态跳变。
"""

from __future__ import annotations

from typing import Final

from agent_core.protocol.runtime import RunContext, RunStatus


class RunStateError(RuntimeError):
    """Run 状态流转非法时抛出。"""


RUN_STATUS_TRANSITIONS: Final[dict[RunStatus, frozenset[RunStatus]]] = {
    "queued": frozenset({"running", "interrupted", "cancelled", "failed"}),
    "running": frozenset({
        "waiting_retry",
        "waiting_approval",
        "waiting_clarification",
        "interrupted",
        "completed",
        "failed",
        "cancelled",
    }),
    "waiting_retry": frozenset({"queued", "running", "interrupted", "cancelled", "failed"}),
    "waiting_approval": frozenset({"queued", "running", "interrupted", "cancelled"}),
    "waiting_clarification": frozenset({"queued", "running", "interrupted", "cancelled"}),
    "interrupted": frozenset({"queued", "running", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset({"queued"}),
    "cancelled": frozenset(),
}


def can_transition_run_status(current: RunStatus, target: RunStatus) -> bool:
    """判断状态是否允许流转；同状态视为幂等成功。"""
    return current == target or target in RUN_STATUS_TRANSITIONS[current]


def transition_run_status(context: RunContext, target: RunStatus) -> None:
    """校验并写入 RunContext.status。

    这是 Runtime 内部修改 run 状态的唯一入口；调用方仍负责发出对应的
    RunEvent 和持久化。
    """
    current = context.status
    if not can_transition_run_status(current, target):
        raise RunStateError(f"run 状态不允许从 {current} 流转到 {target}")
    context.status = target
