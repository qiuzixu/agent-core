"""资源级 RBAC 中间件：身份强制 + 角色→工具细粒度映射。

与 ``AccessContext.can_access``（存储层的资源归属校验）互补，本中间件在
Agent 执行层强制：

1. ``strict_identity=True`` 时拒绝匿名/缺失身份的运行；
2. ``role_tools`` 声明"角色 → 可调用工具集合"；请求者的角色集合均未命中
   映射时视为未授权（admin/tenant_admin 默认旁路）。

身份来源：``MiddlewareContext.metadata``（HTTP 装配层把身份头写入
run_metadata 后会自动流入）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

_ADMIN_ROLES = frozenset({"admin", "tenant_admin"})


def _parse_roles(value: Any) -> frozenset[str]:
    """把 str / list / set 形式的角色字段归一化为集合。"""
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(item).strip() for item in value if str(item).strip())
    return frozenset()


@dataclass
class ResourceRbacConfig:
    """资源级 RBAC 配置。"""

    enabled: bool = False
    strict_identity: bool = False
    # 角色 → 允许的工具名集合；未声明的角色回退到 "user" 的授权。
    role_tools: dict[str, frozenset[str]] = field(default_factory=dict)
    admin_bypass: bool = True


class ResourceRbacMiddleware(Middleware):
    priority = 5
    """Agent 层资源级权限中间件。"""

    def __init__(self, config: ResourceRbacConfig | None = None) -> None:
        self._config = config or ResourceRbacConfig()

    @staticmethod
    def _identity(metadata: dict[str, Any]) -> tuple[str, str, frozenset[str]]:
        user_id = str(metadata.get("user_id") or metadata.get("access_user_id") or "").strip()
        tenant_id = str(metadata.get("tenant_id") or metadata.get("access_tenant_id") or "").strip()
        roles = _parse_roles(
            metadata.get("roles")
            or metadata.get("user_roles")
            or metadata.get("access_roles")
        )
        return user_id, tenant_id, roles or frozenset({"user"})

    def _tool_allowed(self, tool_name: str, roles: frozenset[str]) -> bool:
        mapping = self._config.role_tools
        if not mapping:
            return True
        if self._config.admin_bypass and roles & _ADMIN_ROLES:
            return True
        allowed: set[str] = set()
        matched = False
        for role in roles:
            granted = mapping.get(role)
            if granted is None:
                granted = mapping.get("user", frozenset())
            if granted:
                matched = True
                allowed.update(granted)
        if not matched:
            return False
        return tool_name in allowed

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        user_id, tenant_id, roles = self._identity(context.metadata)
        if self._config.strict_identity and (not user_id or user_id == "anonymous"):
            logger.warning("RBAC 拒绝匿名运行：缺少有效身份")
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error="缺少有效身份，拒绝执行（RBAC strict_identity）",
            )
        context.metadata["rbac_user_id"] = user_id
        context.metadata["rbac_tenant_id"] = tenant_id
        context.metadata["rbac_roles"] = sorted(roles)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        tool_name = context.tool_name
        if not tool_name:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        _, _, roles = self._identity(context.metadata)
        if not self._tool_allowed(tool_name, roles):
            logger.warning("RBAC 阻止角色 %s 调用工具 %s", sorted(roles), tool_name)
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error=f"角色 {sorted(roles)} 无权调用工具 {tool_name}",
            )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


__all__ = ["ResourceRbacConfig", "ResourceRbacMiddleware"]