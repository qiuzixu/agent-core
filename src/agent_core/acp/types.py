"""ACP 适配器的公共协议。

ACP（Agent Client Protocol）使用 JSON-RPC 双向通信。这里定义的接口不依赖
某个 Agent 框架，业务项目只需把自己的会话和运行时接到 ``AcpBackend``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

JsonObject = dict[str, Any]


@dataclass
class AcpSession:
    """ACP 会话与业务运行时会话之间的映射。"""

    session_id: str
    cwd: str
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class AcpCapabilities:
    """ACP 服务端向 Client 声明的能力集合。

    ``load_session`` 控制 ``session/load`` 恢复会话是否可用；其余字段对应
    prompt 多媒体与 MCP 传输能力。默认全部关闭，保持最小安全面。
    """

    load_session: bool = False
    image: bool = False
    audio: bool = False
    embedded_context: bool = False
    mcp_http: bool = False
    mcp_sse: bool = False


@dataclass(frozen=True)
class AcpUpdate:
    """业务运行时向 ACP Client 推送的一条标准化更新。"""

    kind: str
    payload: JsonObject = field(default_factory=dict)

    @classmethod
    def text(cls, text: str) -> AcpUpdate:
        """构造可显示给用户的 Agent 文本片段。"""
        return cls("text", {"text": text})

    @classmethod
    def thought(cls, text: str) -> AcpUpdate:
        """构造可选展示的运行过程说明。"""
        return cls("thought", {"text": text})


AcpEmitter = Callable[[AcpUpdate], Awaitable[None]]
AcpPermissionRequester = Callable[[JsonObject], Awaitable[str]]


class AcpBackend(Protocol):
    """将具体 Agent Runtime 适配为 ACP 会话服务。

    可选属性 ``capabilities: AcpCapabilities`` 用于声明更细的协议能力；
    未提供时服务端回退读取 ``supports_load_session``，保持向后兼容。
    """

    agent_name: str
    agent_version: str
    supports_load_session: bool

    async def create_session(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[JsonObject],
    ) -> AcpSession: ...

    async def load_session(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[JsonObject],
    ) -> AcpSession: ...

    async def prompt(
        self,
        session: AcpSession,
        text: str,
        emit: AcpEmitter,
        request_permission: AcpPermissionRequester,
    ) -> JsonObject: ...

    async def cancel(self, session: AcpSession) -> None: ...


__all__ = [
    "AcpBackend",
    "AcpCapabilities",
    "AcpEmitter",
    "AcpPermissionRequester",
    "AcpSession",
    "AcpUpdate",
    "JsonObject",
]
