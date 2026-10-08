"""ACP JSON-RPC stdio 服务端。

该实现覆盖通用 Agent Client Protocol 会话流程：初始化、创建/恢复会话、发送提示、
流式更新、取消和人工审批。传输层只处理 JSON-RPC，实际执行由 ``AcpBackend`` 注入。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import TextIO

from agent_core.acp.types import AcpBackend, AcpSession, AcpUpdate, JsonObject


class AcpProtocolError(ValueError):
    """客户端发送了不符合 ACP/JSON-RPC 要求的请求。"""


class AcpSessionNotFoundError(LookupError):
    """请求引用了当前 ACP 服务端不存在的会话。"""

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.session_id = session_id


class AcpStdioServer:
    """基于标准输入输出运行的 ACP 双向 JSON-RPC 服务端。"""

    protocol_version = 1

    def __init__(
        self,
        backend: AcpBackend,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        approval_timeout_seconds: float = 300.0,
    ) -> None:
        if approval_timeout_seconds <= 0:
            raise ValueError("approval_timeout_seconds 必须大于 0")
        self._backend = backend
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._approval_timeout_seconds = approval_timeout_seconds
        self._sessions: dict[str, AcpSession] = {}
        self._pending_client_requests: dict[str, asyncio.Future[JsonObject]] = {}
        self._prompt_tasks: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._write_lock = asyncio.Lock()

    async def run(self) -> None:
        """持续处理 ACP Client 写入的 JSON-RPC 行，直到 stdin 关闭。"""
        try:
            while line := await asyncio.to_thread(self._stdin.readline):
                await self._handle_line(line)
        finally:
            # 任务完成回调会从集合移除自身，先创建快照避免遍历期间修改集合。
            for task in tuple(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _handle_line(self, line: str) -> None:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            await self._send_error(None, -32700, "JSON-RPC 解析失败")
            return
        if not isinstance(value, dict):
            await self._send_error(None, -32600, "JSON-RPC 请求必须是对象")
            return
        if "method" not in value:
            self._resolve_client_response(value)
            return
        method = value.get("method")
        if not isinstance(method, str):
            await self._send_error(value.get("id"), -32600, "method 必须是字符串")
            return
        params = value.get("params", {})
        if not isinstance(params, dict):
            await self._send_error(value.get("id"), -32602, "params 必须是对象")
            return
        task = asyncio.create_task(self._handle_request(value.get("id"), method, params))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _resolve_client_response(self, value: JsonObject) -> None:
        request_id = value.get("id")
        if not isinstance(request_id, str):
            return
        future = self._pending_client_requests.pop(request_id, None)
        if future is None or future.done():
            return
        error = value.get("error")
        if isinstance(error, dict):
            future.set_exception(AcpProtocolError(str(error.get("message") or "ACP Client 拒绝请求")))
            return
        result = value.get("result", {})
        future.set_result(result if isinstance(result, dict) else {"value": result})

    async def _handle_request(self, request_id: object, method: str, params: JsonObject) -> None:
        is_request = request_id is not None
        try:
            result = await self._dispatch(method, params)
        except asyncio.CancelledError:
            if is_request:
                await self._send_result(request_id, {"stopReason": "cancelled"})
        except AcpProtocolError as exc:
            if is_request:
                await self._send_error(request_id, -32602, str(exc))
        except AcpSessionNotFoundError as exc:
            if is_request:
                await self._send_error(request_id, -32004, f"会话不存在：{exc.session_id}")
        except NotImplementedError as exc:
            if is_request:
                await self._send_error(request_id, -32601, str(exc))
        except Exception as exc:
            if is_request:
                await self._send_error(request_id, -32603, f"ACP Agent 执行失败：{exc}")
        else:
            if is_request:
                await self._send_result(request_id, result)

    async def _dispatch(self, method: str, params: JsonObject) -> JsonObject:
        if method == "initialize":
            return self._initialize(params)
        if method == "session/new":
            return await self._new_session(params)
        if method == "session/load":
            if not getattr(self._backend, "supports_load_session", False):
                raise NotImplementedError("当前 Agent 不支持 ACP 会话恢复")
            return await self._load_session(params)
        if method == "session/prompt":
            return await self._prompt(params)
        if method == "session/cancel":
            return await self._cancel(params)
        raise NotImplementedError(f"不支持的 ACP 方法：{method}")

    def _initialize(self, params: JsonObject) -> JsonObject:
        client_version = params.get("protocolVersion", self.protocol_version)
        if not isinstance(client_version, int):
            raise AcpProtocolError("protocolVersion 必须是整数")
        if client_version != self.protocol_version:
            raise AcpProtocolError(f"不支持的 ACP 版本：{client_version}，当前仅支持 {self.protocol_version}")
        return {
            "protocolVersion": self.protocol_version,
            "agentCapabilities": {
                "loadSession": bool(getattr(self._backend, "supports_load_session", False)),
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
                "mcpCapabilities": {"http": False, "sse": False},
            },
            "agentInfo": {
                "name": self._backend.agent_name,
                "title": self._backend.agent_name,
                "version": self._backend.agent_version,
            },
        }

    async def _new_session(self, params: JsonObject) -> JsonObject:
        cwd = self._require_string(params, "cwd")
        session_id = str(uuid.uuid4())
        session = await self._backend.create_session(session_id, cwd, self._mcp_servers(params))
        self._sessions[session.session_id] = session
        return {"sessionId": session.session_id, "modes": [], "configOptions": []}

    async def _load_session(self, params: JsonObject) -> JsonObject:
        session_id = self._require_string(params, "sessionId")
        cwd = self._require_string(params, "cwd")
        session = await self._backend.load_session(session_id, cwd, self._mcp_servers(params))
        self._sessions[session.session_id] = session
        return {"sessionId": session.session_id, "modes": [], "configOptions": []}

    async def _prompt(self, params: JsonObject) -> JsonObject:
        session_id = self._require_string(params, "sessionId")
        session = self._session(session_id)
        if session_id in self._prompt_tasks:
            raise AcpProtocolError("该 ACP 会话已有正在执行的提示")
        text = self._prompt_text(params.get("prompt"))
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("无法获取 ACP 提示任务")
        self._prompt_tasks[session_id] = task
        try:
            return await self._backend.prompt(
                session,
                text,
                lambda update: self._emit_update(session_id, update),
                lambda request: self._request_permission(session_id, request),
            )
        finally:
            self._prompt_tasks.pop(session_id, None)

    async def _cancel(self, params: JsonObject) -> JsonObject:
        session_id = self._require_string(params, "sessionId")
        session = self._session(session_id)
        await self._backend.cancel(session)
        task = self._prompt_tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()
        return {}

    async def _emit_update(self, session_id: str, update: AcpUpdate) -> None:
        payload = self._update_payload(update)
        if payload is None:
            return
        await self._send_notification("session/update", {"sessionId": session_id, "update": payload})

    async def _request_permission(self, session_id: str, request: JsonObject) -> str:
        options = request.get("options")
        if not isinstance(options, list) or not options:
            options = [
                {"optionId": "approve_once", "name": "批准", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "拒绝", "kind": "reject_once"},
            ]
        allowed_option_ids = {
            option["optionId"]
            for option in options
            if isinstance(option, dict) and isinstance(option.get("optionId"), str) and option["optionId"]
        }
        if not allowed_option_ids:
            raise AcpProtocolError("审批选项必须包含非空 optionId")
        result = await self._request_client(
            "session/request_permission",
            {
                "sessionId": session_id,
                "toolCall": request.get("toolCall", {}),
                "options": options,
            },
        )
        for key in ("selectedOptionId", "optionId", "outcome"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value if value in allowed_option_ids else "reject_once"
        return "reject_once"

    async def _request_client(self, method: str, params: JsonObject) -> JsonObject:
        request_id = f"agent-{uuid.uuid4()}"
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending_client_requests[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=self._approval_timeout_seconds)
        except TimeoutError:
            return {"selectedOptionId": "reject_once"}
        finally:
            self._pending_client_requests.pop(request_id, None)

    @staticmethod
    def _update_payload(update: AcpUpdate) -> JsonObject | None:
        text = update.payload.get("text")
        if update.kind == "text" and isinstance(text, str):
            return {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            }
        if update.kind == "thought" and isinstance(text, str):
            return {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": text},
            }
        if update.kind == "tool_started":
            return {
                "sessionUpdate": "tool_call",
                "toolCallId": str(update.payload.get("toolCallId") or uuid.uuid4()),
                "title": str(update.payload.get("title") or "调用工具"),
                "kind": "other",
                "status": "in_progress",
                "rawInput": update.payload.get("input", {}),
            }
        if update.kind == "tool_finished":
            return {
                "sessionUpdate": "tool_call_update",
                "toolCallId": str(update.payload.get("toolCallId") or "tool"),
                "status": "completed" if update.payload.get("success", True) else "failed",
                "content": update.payload.get("content", []),
            }
        return None

    @staticmethod
    def _prompt_text(prompt: object) -> str:
        if not isinstance(prompt, list):
            raise AcpProtocolError("prompt 必须是内容块数组")
        texts: list[str] = []
        for block in prompt:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        value = "\n".join(texts).strip()
        if not value:
            raise AcpProtocolError("prompt 中缺少文本内容")
        return value

    @staticmethod
    def _mcp_servers(params: JsonObject) -> list[JsonObject]:
        value = params.get("mcpServers", [])
        if not isinstance(value, list):
            raise AcpProtocolError("mcpServers 必须是数组")
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _require_string(params: JsonObject, key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value.strip():
            raise AcpProtocolError(f"{key} 必须是非空字符串")
        return value.strip()

    def _session(self, session_id: str) -> AcpSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise AcpSessionNotFoundError(session_id)
        return session

    async def _send_result(self, request_id: object, result: JsonObject) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _send_error(self, request_id: object, code: int, message: str) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    async def _send_notification(self, method: str, params: JsonObject) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, payload: JsonObject) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            await asyncio.to_thread(self._stdout.write, line)
            await asyncio.to_thread(self._stdout.flush)


async def run_acp_stdio(backend: AcpBackend) -> None:
    """使用默认标准输入输出启动 ACP 服务。"""
    await AcpStdioServer(backend).run()


__all__ = [
    "AcpProtocolError",
    "AcpSessionNotFoundError",
    "AcpStdioServer",
    "run_acp_stdio",
]
