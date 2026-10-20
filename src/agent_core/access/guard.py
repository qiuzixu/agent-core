"""访问校验的共享实现。

为什么单独抽一个函数？
    ``AgentRuntime`` 与 ``DurableWorkflowRunner`` 都在读取/恢复入口执行
    同一套三类校验。抽到这里做唯一实现，避免两份拷贝各自漂移。

三类校验（按顺序短路，第一个失败就抛异常）：
    1. 身份存在   —— 严格模式下必须提供 AccessContext（你是谁？）
    2. 资源有归属 —— 资源必须绑定了 owner_user_id 和 owner_tenant_id
                     （这条资源属于谁？）
    3. 有权访问   —— 调用者的 can_access() 必须通过
                     （你有没有权看这条资源？）

典型调用场景：
    用户 A 尝试读取用户 B 的对话线程 →
    enforce_access(strict=True, ..., owner_user_id="B", access=ctx_A)
    → can_access("B", ...) 返回 False
    → PermissionError("无权访问该对话")
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
        strict: 严格模式开关（``require_access``）。为 False 时跳过前两步，
                只做第三步归属检查。
        accessor: 严格模式错误文案中的执行体名称，如 "AgentRuntime"。
        subject: 资源对象文案，如 "run" / "工作流执行实例"。
        owner_user_id: 资源绑定的用户 ID；None 表示未绑定。
        owner_tenant_id: 资源绑定的租户 ID；None 表示未绑定。
        access: 调用方身份；None 表示匿名访问（严格模式下不允许）。

    Raises:
        PermissionError: 三类校验中任何一步不通过。
    """
    # ── 第 1 步：身份存在 ────────────────────────────────────
    # 严格模式下不传 access（匿名调用）直接拒绝。
    if strict and access is None:
        raise PermissionError(f"该 {accessor} 要求提供 AccessContext")

    # ── 第 2 步：资源有归属 ──────────────────────────────────
    # 资源没有绑定 user_id 或 tenant_id 时不允许访问，
    # 防止"忘记设置归属"导致资源意外变成公共可见。
    if strict and (owner_user_id is None or owner_tenant_id is None):
        raise PermissionError(f"该 {subject} 尚未绑定用户和租户")

    # ── 第 3 步：有权访问 ────────────────────────────────────
    # 调用者必须能访问该资源的归属者（管理员或本人）。
    if access is not None and not access.can_access(owner_user_id, owner_tenant_id):
        raise PermissionError(f"无权访问该 {subject}")


__all__ = ["enforce_access"]
