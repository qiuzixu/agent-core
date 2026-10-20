"""通用访问上下文。

设计理念：
    AccessContext 不做认证（JWT 解析、HTTP Header 提取、登录），
    只做授权——即"已经解析好的身份"能否访问某条资源。

    认证由 HTTP 层或网关完成；本模块只消费认证结果。

角色约定：
    - "admin" / "tenant_admin"：租户级管理员，可看本租户内所有资源。
    - 其他角色：普通用户，只能看自己名下的资源（或无归属的公共资源）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# 管理员角色的集合；roles 中任意命中即视为管理员。
_ADMIN_ROLES = frozenset({"admin", "tenant_admin"})


@dataclass(frozen=True)
class AccessContext:
    """一次请求的访问身份（不可变值对象）。

    典型用法：
        ctx = AccessContext(user_id="张三", tenant_id="T1", roles={"user"})
        ctx.can_access("李四", "T1")  # False：张三不能看李四的资源
        ctx.can_access("张三", "T1")  # True：自己的资源当然可以看

    Attributes:
        user_id:   当前请求的用户标识；匿名时为 "anonymous"。
        tenant_id: 当前请求所属的租户标识；用于多租户隔离。
        roles:     角色集合（不可变）；用于判断是否为管理员等。
    """

    # 默认匿名——本地开发或测试时不想操心权限，直接不传即可。
    user_id: str = "anonymous"
    # 默认租户——单租户部署时不需要显式指定。
    tenant_id: str = "default"
    # 默认普通用户——没有特殊权限，只能访问自己的资源。
    roles: frozenset[str] = field(default_factory=lambda: frozenset({"user"}))

    @property
    def is_admin(self) -> bool:
        """判断是否拥有当前租户范围内的管理权限。

        只要 roles 中包含 "admin" 或 "tenant_admin" 任意一个，
        就视为管理员。
        """
        return bool(_ADMIN_ROLES & self.roles)

    def can_access(
        self,
        owner_user_id: str | None,
        owner_tenant_id: str | None,
    ) -> bool:
        """判断当前身份是否可以访问指定归属的资源。

        判断规则（按顺序）：
        1. 管理员 + 资源在本租户（或无租户归属）→ 允许；
        2. 资源无归属（owner_user_id=None）→ 视为公共资源，允许；
        3. 资源属于当前用户 + 当前租户 → 允许；
        4. 其余情况 → 拒绝。

        Args:
            owner_user_id:   资源绑定的用户 ID；None 表示无归属。
            owner_tenant_id: 资源绑定的租户 ID；None 表示无租户限制。

        Returns:
            True 表示允许访问，False 表示拒绝。
        """
        # 管理员可以看本租户内的所有资源（但不能跨租户）。
        if self.is_admin and owner_tenant_id in (None, self.tenant_id):
            return True
        # 普通用户：资源必须属于自己（或无归属），且租户一致（或无租户限制）。
        return owner_user_id in (None, self.user_id) and owner_tenant_id in (None, self.tenant_id)

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化或写入事件的字典。

        frozenset 转为 sorted list，保证 JSON 序列化结果稳定可复现。
        """
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "roles": sorted(self.roles),
        }


__all__ = ["AccessContext"]
