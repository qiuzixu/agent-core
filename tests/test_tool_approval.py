"""工具审批边界测试。"""

from __future__ import annotations

import pytest

from agent_core.tools import ToolExecutor, ToolRegistry


@pytest.mark.asyncio
async def test_preapproved_call_only_bypasses_approval_for_current_execution() -> None:
    calls: list[str] = []

    async def write_value(value: str) -> str:
        calls.append(value)
        return value

    registry = ToolRegistry()
    registry.register(
        "write_value",
        write_value,
        "写入值",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        requires_approval=True,
    )
    executor = ToolExecutor(registry)

    blocked = await executor.execute_result("write_value", {"value": "未审批"})
    approved = await executor.execute_result(
        "write_value",
        {"value": "已审批"},
        approval_granted=True,
    )

    assert blocked.success is False
    assert blocked.error_kind == "approval"
    assert approved.success is True
    assert approved.value == "已审批"
    assert calls == ["已审批"]


@pytest.mark.asyncio
async def test_explicit_rejection_does_not_call_approval_callback() -> None:
    callback_called = False

    async def approval_callback(_spec: object, _arguments: dict[str, object]) -> bool:
        nonlocal callback_called
        callback_called = True
        return True

    registry = ToolRegistry()
    registry.register("dangerous", lambda: "done", "危险操作", requires_approval=True)
    executor = ToolExecutor(registry, approval_callback=approval_callback)  # type: ignore[arg-type]

    result = await executor.execute_result("dangerous", {}, approval_granted=False)

    assert result.success is False
    assert result.error_kind == "approval"
    assert callback_called is False
