"""访问校验的共享实现。

``AgentRuntime`` 与 ``DurableWorkflowRunner`` 都在读取/恢复入口执行同一套
三类校验：严格模式必须提供身份、资源必须绑定归属、访问者必须有权。
这里把判定顺序与错误文案固定在唯一实现处，避免两份拷贝各自漂移。
"""

from __future__ import annotations

from agent_core.access.context import AccessContext


def enforce_access(
    *,
    strict: bool,
    accessor: str,
    subject: str,
    owner_user_id: str | None,
    owner_tenant_id: str | None,
    access: AccessContext | None,
) -> None:
    """统一执行三类访问校验，任一不通过时抛出 ``PermissionError``。

    Args:
        strict: 严格模式开关（``require_access``）。
        accessor: 严格模式错误文案中的执行体名称，如 "AgentRuntime"。
        subject: 资源对象文案，如 "run" / "工作流执行实例"。
        owner_user_id: 资源绑定的用户 ID。
        owner_tenant_id: 资源绑定的租户 ID。
        access: 调用方身份；None 表示匿名访问（严格模式下不允许）。
    """
    if strict and access is None:
        raise PermissionError(f"该 {accessor} 要求提供 AccessContext")
    if strict and (owner_user_id is None or owner_tenant_id is None):
        raise PermissionError(f"该 {subject} 尚未绑定用户和租户")
    if access is not None and not access.can_access(owner_user_id, owner_tenant_id):
        raise PermissionError(f"无权访问该 {subject}")


__all__ = ["enforce_access"]
