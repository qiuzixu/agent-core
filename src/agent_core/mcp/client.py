"""MCP 客户端适配器（支持 stdio / HTTP / SSE 传输）。

Core 提供标准 MCP 会话管理和工具调用，不包含任何业务语义。
应用层可以基于 McpToolCaller 包装自己的受限客户端。

传输方式：
- stdio：本地子进程（原有能力）
- http：远程 Streamable HTTP（MCP 1.30+ 推荐方式）
- sse：远程 Server-Sent Events（旧版兼容）

自动重连：
    会话断开后，下一次调用会自动尝试重启连接，
    重试次数由 max_reconnects 控制（默认 3 次，0 = 不重连）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal, Protocol, cast

from agent_core.mcp.errors import McpConnectionError, McpTimeoutError, McpToolError

logger = logging.getLogger(__name__)

JsonObject = dict[str, object]
McpTransport = Literal["stdio", "http", "sse"]

_INHERITED_ENV_KEYS = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
    }
)


class McpToolCaller(Protocol):
    """MCP 工具调用端口。"""

    async def list_tools(self) -> list[str]: ...

    async def list_tool_definitions(self) -> list[JsonObject]: ...

    async def call_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> JsonObject: ...


class McpToolClient:
    """按需启动 MCP 会话（stdio 子进程或远程 HTTP/SSE），并在生命周期内复用同一个会话。

    支持三种传输方式：stdio（本地）、http（Streamable HTTP）、sse（Server-Sent Events）。
    会话断开后自动尝试重连（次数由 max_reconnects 控制）。

    重连生命周期回调（可选）：
    - on_reconnected: 重连成功后触发，上层可借此自动刷新工具清单；
    - on_give_up: 重连预算耗尽（连续失败且不再尝试）时触发一次，
      上层可借此把面板状态标记为 error，避免"僵尸在线"。
    """

    def __init__(
        self,
        *,
        command: str = "",
        entrypoint: Path | None = None,
        timeout_seconds: float = 30.0,
        env: Mapping[str, str] | None = None,
        transport: McpTransport = "stdio",
        url: str = "",
        headers: Mapping[str, str] | None = None,
        max_reconnects: int = 3,
        reconnect_delay_seconds: float = 1.0,
        on_reconnected: Callable[[], Awaitable[None]] | None = None,
        on_give_up: Callable[[Exception], Awaitable[None]] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self._transport: McpTransport = transport
        self._command = command
        self._entrypoint = entrypoint
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})
        self._url = url
        self._headers = dict(headers or {})
        self._max_reconnects = max(0, max_reconnects)
        self._reconnect_delay_seconds = max(0.0, reconnect_delay_seconds)
        self._session: object | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._close_requested: asyncio.Event | None = None
        self._terminal_error: McpConnectionError | McpTimeoutError | None = None
        self._reconnect_count: int = 0
        self._on_reconnected = on_reconnected
        self._on_give_up = on_give_up
        # 放弃通知只发一次，避免预算耗尽后每次调用都重复触发上层回调。
        self._give_up_notified = False
        self._start_lock = asyncio.Lock()

    @property
    def transport(self) -> str:
        """当前传输方式。"""
        return self._transport

    async def start(self) -> None:
        """启动会话并等待 MCP initialize 握手完成。"""
        if self._session is not None:
            return
        async with self._start_lock:
            if self._session is not None:
                return
            if self._transport == "stdio":
                entrypoint = self._entrypoint
                if entrypoint is None:
                    raise McpConnectionError("stdio 传输必须提供 entrypoint")
                if not await asyncio.to_thread(entrypoint.is_file):
                    raise McpConnectionError(f"MCP 入口不存在：{entrypoint}")
            elif not self._url:
                raise McpConnectionError(f"{self._transport} 传输必须提供 url")
            if self._runner_task is None or self._runner_task.done():
                loop = asyncio.get_running_loop()
                self._ready = loop.create_future()
                self._close_requested = asyncio.Event()
                self._terminal_error = None
                self._reconnect_count = 0
                session_name = self._entrypoint.name if self._entrypoint else self._url
                self._runner_task = asyncio.create_task(
                    self._run_session(self._ready, self._close_requested),
                    name=f"mcp-session:{session_name}",
                )
            ready = self._ready
        if ready is None:
            raise McpConnectionError("MCP 会话启动状态无效")
        await asyncio.shield(ready)

    async def list_tools(self) -> list[str]:
        """列出 MCP 服务公开的工具名称。"""
        session = await self._require_session()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await session.list_tools()  # type: ignore[attr-defined]
        except TimeoutError as exc:
            raise McpTimeoutError("MCP tools/list 超时") from exc
        except Exception as exc:
            raise McpToolError(f"MCP tools/list 调用失败：{exc}") from exc
        return [str(tool.name) for tool in result.tools]

    async def list_tool_definitions(self) -> list[JsonObject]:
        """列出 MCP 服务的完整工具定义（含 inputSchema），供自动注册使用。"""
        session = await self._require_session()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await session.list_tools()  # type: ignore[attr-defined]
        except TimeoutError as exc:
            raise McpTimeoutError("MCP tools/list 超时") from exc
        except Exception as exc:
            raise McpToolError(f"MCP tools/list 调用失败：{exc}") from exc
        definitions: list[JsonObject] = []
        for tool in result.tools:
            definitions.append({
                "name": str(tool.name),
                "description": getattr(tool, "description", None) or "",
                "inputSchema": getattr(tool, "inputSchema", None) or {
                    "type": "object", "properties": {}, "required": []
                },
            })
        return definitions

    async def call_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> JsonObject:
        """调用 MCP 工具并返回结构化结果。"""
        session = await self._require_session()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await session.call_tool(name, dict(arguments or {}))  # type: ignore[attr-defined]
        except TimeoutError as exc:
            raise McpTimeoutError(f"MCP 工具 {name} 调用超时") from exc
        except Exception as exc:
            raise McpToolError(f"MCP 工具 {name} 调用失败：{exc}") from exc
        if result.isError:
            message = self._text_content(result.content) or "服务返回 isError=true"
            raise McpToolError(f"MCP 工具 {name} 失败：{message}")
        structured = result.structuredContent
        if isinstance(structured, dict):
            # \u5982\u679c\u5de5\u5177\u5b9a\u4e49\u4e86 outputSchema\uff0c\u505a\u8f7b\u91cf\u7ea7\u6821\u9a8c
            output_schema = getattr(result, "outputSchema", None)
            if output_schema is not None:
                error = self._validate_output_schema(structured, output_schema)
                if error:
                    logger.warning("MCP \u5de5\u5177 %s \u8f93\u51fa\u6821\u9a8c\u8b66\u544a\uff1a%s", name, error)
            return cast(JsonObject, structured)
        text = self._text_content(result.content)
        if not text:
            raise McpToolError(f"MCP 工具 {name} 未返回可读取的内容")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {"content": text}
        if isinstance(decoded, dict):
            return cast(JsonObject, decoded)
        return {"content": decoded}

    async def aclose(self) -> None:
        """关闭 MCP 会话及其 stdio 子进程或远程连接。"""
        async with self._start_lock:
            task = self._runner_task
            close_requested = self._close_requested
            self._runner_task = None
            self._ready = None
            self._close_requested = None
        if task is None:
            return
        if close_requested is not None:
            close_requested.set()
        await asyncio.shield(task)

    async def __aenter__(self) -> McpToolClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def _require_session(self) -> object:
        """获取当前会话；会话不可用时尝试自动重连。"""
        if (
            self._session is None
            and self._terminal_error is not None
            and self._reconnect_count < self._max_reconnects
        ):
            self._reconnect_count += 1
            logger.info(
                "MCP 会话断开，第 %d/%d 次重连（transport=%s）",
                self._reconnect_count, self._max_reconnects, self._transport,
            )
            if self._reconnect_delay_seconds > 0:
                await asyncio.sleep(self._reconnect_delay_seconds)
            self._terminal_error = None
            self._runner_task = None
            try:
                await self.start()
            except (McpConnectionError, McpTimeoutError):
                logger.warning("MCP 重连失败（第 %d 次）", self._reconnect_count)
                raise
            # 重连成功：重置放弃通知标记，并通知上层（如自动刷新工具集）。
            self._give_up_notified = False
            if self._on_reconnected is not None:
                try:
                    await self._on_reconnected()
                except Exception:
                    logger.exception("MCP on_reconnected 回调执行失败")
        await self.start()
        if self._session is not None:
            return self._session
        if self._terminal_error is not None:
            await self._notify_give_up(self._terminal_error)
            raise self._terminal_error
        raise McpConnectionError("MCP 会话初始化失败")

    async def _notify_give_up(self, error: Exception) -> None:
        """重连预算耗尽后一次性通知上层（例如把管理面板状态置为 error）。"""
        if self._on_give_up is None or self._give_up_notified:
            return
        self._give_up_notified = True
        try:
            await self._on_give_up(error)
        except Exception:
            logger.exception("MCP on_give_up 回调执行失败")

    async def _run_session(self, ready: asyncio.Future[None], close_requested: asyncio.Event) -> None:
        """在同一协程内持有 MCP 的异步上下文，保证会话生命周期完整。"""
        stack = AsyncExitStack()
        try:
            try:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.sse import sse_client
                from mcp.client.stdio import stdio_client
                from mcp.client.streamable_http import streamablehttp_client
            except ImportError as exc:
                raise McpConnectionError("MCP 适配器需要安装 agent-core[mcp]") from exc

            if self._transport == "stdio":
                params = StdioServerParameters(
                    command=self._command,
                    args=[str(self._entrypoint)],
                    # 只继承启动子进程必需的系统变量，避免把模型密钥等无关私密透传给 MCP。
                    env={
                        **{key: value for key, value in os.environ.items() if key.upper() in _INHERITED_ENV_KEYS},
                        **self._env,
                    },
                )
                async with asyncio.timeout(self._timeout_seconds):
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
            elif self._transport == "http":
                async with asyncio.timeout(self._timeout_seconds):
                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(self._url, headers=self._headers)
                    )
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
            else:  # sse
                async with asyncio.timeout(self._timeout_seconds):
                    read, write = await stack.enter_async_context(
                        sse_client(self._url, headers=self._headers)
                    )
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()

            self._session = session
            self._reconnect_count = 0
            ready.set_result(None)
            await close_requested.wait()
        except TimeoutError:
            timeout_error = McpTimeoutError(
                f"MCP 初始化超时：{self._entrypoint.name if self._entrypoint else self._url}"
            )
            self._terminal_error = timeout_error
            if not ready.done():
                ready.set_exception(timeout_error)
        except BaseException as exc:
            connection_error = (
                exc
                if isinstance(exc, McpConnectionError)
                else McpConnectionError(
                    f"无法启动或维持 MCP 服务："
                    f"{self._entrypoint if self._entrypoint else self._url}（{exc}）"
                )
            )
            self._terminal_error = connection_error
            if not ready.done():
                ready.set_exception(connection_error)
        finally:
            self._session = None
            try:
                await stack.aclose()
            except BaseException as exc:
                self._terminal_error = McpConnectionError(f"关闭 MCP 服务失败：{exc}")

    @staticmethod
    def _validate_output_schema(result: object, schema: dict[str, object] | None) -> str | None:
        """\u8f7b\u91cf\u7ea7 outputSchema \u6821\u9a8c\uff08\u4ec5\u68c0\u67e5\u9876\u5c42\u7c7b\u578b\u548c required \u5b57\u6bb5\uff09\u3002

        \u8fd4\u56de\u9519\u8bef\u4fe1\u606f\uff08\u65e0\u9519\u8bef\u8fd4\u56de None\uff09\u3002
        """
        if not schema or not isinstance(schema, dict):
            return None
        if not isinstance(result, dict):
            expected = schema.get("type", "object")
            if expected == "object":
                return f"\u8f93\u51fa\u7c7b\u578b\u4e0d\u5339\u914d\uff1a\u671f\u671b object\uff0c\u5b9e\u9645 {type(result).__name__}"
            return None
        properties = schema.get("properties", {})
        # required 在 dict[str, object] 里类型为 object，先收窄为 list 再迭代（双端同步）。
        required = schema.get("required", [])
        if isinstance(properties, dict) and isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in result:
                    return f"\u8f93\u51fa\u7f3a\u5c11\u5fc5\u9700\u5b57\u6bb5\uff1a{key}"
        return None

    @staticmethod
    def _text_content(content: object) -> str | None:
        if not isinstance(content, list):
            return None
        texts = [text for item in content if isinstance((text := getattr(item, "text", None)), str)]
        return "\n".join(texts) if texts else None


__all__ = ["JsonObject", "McpToolCaller", "McpToolClient", "McpTransport"]