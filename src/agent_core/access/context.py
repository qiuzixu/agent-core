"""通用访问上下文。

Core 只表达一次运行所携带的用户、租户和角色边界，不负责 JWT 校验、HTTP
Header 解析或具体认证系统。存储适配器可以使用 ``can_access`` 做资源归属检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AccessContext:
    """一次请求的访问身份。"""

    user_id: str = "anonymous"
    tenant_id: str = "default"
    roles: frozenset[str] = field(default_factory=lambda: frozenset({"user"}))

    @property
    def is_admin(self) -> bool:
        """判断是否拥有当前租户范围内的管理权限。"""
        return bool({"admin", "tenant_admin"} & self.roles)

    def can_access(
        self,
        owner_user_id: str | None,
        owner_tenant_id: str | None,
    ) -> bool:
        """判断当前身份是否可以访问资源。"""
        if self.is_admin and owner_tenant_id in (None, self.tenant_id):
            return True
        return owner_user_id in (None, self.user_id) and owner_tenant_id in (None, self.tenant_id)

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化或写入事件的字典。"""
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "roles": sorted(self.roles),
        }


__all__ = ["AccessContext"]
