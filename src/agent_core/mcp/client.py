"""MCP stdio 客户端适配器。

Core 提供标准 MCP 会话管理和工具调用，不包含任何 Cesium、航线或业务白名单。
应用层可以基于 ``McpToolCaller`` 包装自己的受限客户端。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Protocol, cast

from agent_core.mcp.errors import McpConnectionError, McpTimeoutError, McpToolError

JsonObject = dict[str, object]
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

    async def call_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> JsonObject: ...


class McpToolClient:
    """按需启动 MCP stdio 服务，并在生命周期内复用同一个会话。"""

    def __init__(
        self,
        *,
        command: str,
        entrypoint: Path,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self._command = command
        self._entrypoint = entrypoint
        self._timeout_seconds = timeout_seconds
        self._env = dict(env or {})
        self._session: object | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._close_requested: asyncio.Event | None = None
        self._terminal_error: McpConnectionError | McpTimeoutError | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        """启动会话并等待 MCP initialize 完成。"""
        if self._session is not None:
            return
        async with self._start_lock:
            if self._session is not None:
                return
            if not await asyncio.to_thread(self._entrypoint.is_file):
                raise McpConnectionError(f"MCP 入口不存在：{self._entrypoint}")
            if self._runner_task is None or self._runner_task.done():
                loop = asyncio.get_running_loop()
                self._ready = loop.create_future()
                self._close_requested = asyncio.Event()
                self._terminal_error = None
                self._runner_task = asyncio.create_task(
                    self._run_session(self._ready, self._close_requested),
                    name=f"mcp-session:{self._entrypoint.name}",
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

    async def call_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> JsonObject:
        """调用 MCP 工具并要求其返回结构化对象。"""
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
        """关闭 MCP 会话及其 stdio 子进程。"""
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
        await self.start()
        if self._session is not None:
            return self._session
        if self._terminal_error is not None:
            raise self._terminal_error
        raise McpConnectionError("MCP 会话初始化失败")

    async def _run_session(self, ready: asyncio.Future[None], close_requested: asyncio.Event) -> None:
        """在同一协程内持有 MCP 的异步上下文，保证会话生命周期完整。"""
        stack = AsyncExitStack()
        try:
            try:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client
            except ImportError as exc:
                raise McpConnectionError("MCP stdio 适配器需要安装 agent-core[mcp]") from exc
            params = StdioServerParameters(
                command=self._command,
                args=[str(self._entrypoint)],
                # 只继承启动子进程必需的系统变量，避免把模型密钥等无关秘密透传给 MCP。
                env={
                    **{key: value for key, value in os.environ.items() if key.upper() in _INHERITED_ENV_KEYS},
                    **self._env,
                },
            )
            async with asyncio.timeout(self._timeout_seconds):
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            self._session = session
            ready.set_result(None)
            await close_requested.wait()
        except TimeoutError:
            timeout_error = McpTimeoutError(f"MCP 初始化超时：{self._entrypoint.name}")
            self._terminal_error = timeout_error
            if not ready.done():
                ready.set_exception(timeout_error)
        except BaseException as exc:
            connection_error = (
                exc
                if isinstance(exc, McpConnectionError)
                else McpConnectionError(f"无法启动或维持 MCP 服务：{self._entrypoint}（{exc}）")
            )
            self._terminal_error = connection_error
            if not ready.done():
                ready.set_exception(connection_error)
        finally:
            self._session = None
            try:
                await stack.aclose()
            except BaseException as exc:
                self._terminal_error = McpConnectionError(f"关闭 MCP 服务失败：{self._entrypoint}（{exc}）")

    @staticmethod
    def _text_content(content: object) -> str | None:
        if not isinstance(content, list):
            return None
        texts = [text for item in content if isinstance((text := getattr(item, "text", None)), str)]
        return "\n".join(texts) if texts else None


__all__ = ["JsonObject", "McpToolCaller", "McpToolClient"]
