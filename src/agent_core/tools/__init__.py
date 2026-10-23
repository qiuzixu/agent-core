"""工具注册与执行。"""

from agent_core.protocol.runtime import ToolResult
from agent_core.tools.config import McpDiscoveryConfig
from agent_core.tools.discovery import (
    auto_register_mcp_tools,
    connect_mcp_server,
    mcp_tool_to_function_def,
)
from agent_core.tools.executor import ToolExecutor, ToolRegistry, ToolSpec

__all__ = [
    "McpDiscoveryConfig",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "auto_register_mcp_tools",
    "connect_mcp_server",
    "mcp_tool_to_function_def",
]
