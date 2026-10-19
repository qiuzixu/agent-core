"""工具注册与执行。"""

from agent_core.protocol.runtime import ToolResult
from agent_core.tools.executor import ToolExecutor, ToolRegistry, ToolSpec

__all__ = ["ToolExecutor", "ToolRegistry", "ToolSpec", "ToolResult"]
