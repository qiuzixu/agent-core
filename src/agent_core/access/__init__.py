"""访问上下文公共导出。"""

from agent_core.access.context import AccessContext
from agent_core.access.guard import enforce_access

__all__ = ["AccessContext", "enforce_access"]
