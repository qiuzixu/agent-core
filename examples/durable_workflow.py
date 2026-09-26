"""工作流暂停、持久化和恢复示例。"""

from __future__ import annotations

import asyncio

from agent_core import (
    END,
    START,
    AccessContext,
    DurableWorkflowRunner,
    MemoryWorkflowExecutionStore,
    StateMachineBuilder,
    WorkflowPause,
)

approved = False


async def prepare(state: dict) -> dict:
    """准备执行所需的数据。"""
    return {"prepared": True, "request_id": state["request_id"]}


async def execute(state: dict) -> dict:
    """审批前暂停，审批后继续。"""
    if not approved:
        raise WorkflowPause("等待人工审批", {"request_id": state["request_id"]})
    return {"result": "completed"}


async def main() -> None:
    global approved

    machine = (
        StateMachineBuilder()
        .add_node("prepare", prepare)
        .add_node("execute", execute)
        .add_edge(START, "prepare")
        .add_edge("prepare", "execute")
        .add_edge("execute", END)
        .build()
    )
    access = AccessContext(user_id="user-1", tenant_id="tenant-1")
    runner = DurableWorkflowRunner(
        machine,
        MemoryWorkflowExecutionStore(),
        require_access=True,
    )

    interrupted = await runner.start(
        "demo-workflow",
        {"request_id": "request-42"},
        access=access,
    )
    print("首次执行:", interrupted.status, interrupted.current_step)

    approved = True
    completed = await runner.resume(interrupted.execution_id, access=access)
    print("恢复执行:", completed.status, completed.result_data)


if __name__ == "__main__":
    asyncio.run(main())
