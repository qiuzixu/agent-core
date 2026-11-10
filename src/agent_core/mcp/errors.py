"""MCP 通用错误类型。

这些错误描述连接、超时和工具调用语义，与 Cesium、地图或任何具体业务无关。
具体集成可以在此基础上继续定义领域错误。
"""

from typing import Any

from agent_core.errors import AgentError


class McpError(AgentError):
    """MCP 基础错误。"""


class McpConnectionError(McpError):
    """MCP 连接或会话初始化失败。"""


class McpTimeoutError(McpError):
    """MCP 初始化、列举工具或调用工具超时。"""


class McpToolError(McpError):
    """MCP 服务返回工具执行失败或无效结果。

    ``structured_content`` 保留 isError=true 时的结构化输出（例如地理编码候选），
    供上层按业务语义决定是否展示候选，而不是只看到一段错误文本。
    """

    def __init__(
        self,
        message: str,
        *,
        structured_content: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.structured_content = structured_content


__all__ = ["McpConnectionError", "McpError", "McpTimeoutError", "McpToolError"]
