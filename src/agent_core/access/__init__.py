"""访问控制公共导出。

本模块是 agent-core 的"授权"基石，回答两个问题：
1. 你是谁？      —— ``AccessContext``
2. 你能不能看这条资源？ —— ``AccessContext.can_access`` / ``enforce_access``

它不负责认证（JWT 解析、登录），只负责授权（权限判断）。
被 storage、workflow、HITL、middleware 等模块广泛复用。
"""

from agent_core.access.context import AccessContext
from agent_core.access.guard import enforce_access

__all__ = ["AccessContext", "enforce_access"]
