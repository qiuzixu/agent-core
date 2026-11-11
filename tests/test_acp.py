"""ACP stdio 服务端升级点测试。

覆盖以下能力：
1. ``AcpCapabilities``：initialize 能力声明可配置，且未声明时回退旧的
   ``supports_load_session`` 标记（向后兼容）。
2. ``session/load``：恢复会话时按需回放历史消息。
3. ``session/request_permission``：兼容平铺和嵌套的审批响应。
4. ``session/cancel``：取消会话时立即终结该会话挂起的权限询问，
   不再等待审批超时（fail-closed 语义），且不影响其他会话。
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

from agent_core.acp import (
    AcpCapabilities,
    AcpSession,
    AcpSessionNotFoundError,
    AcpStdioServer,
    AcpUpdate,
)


class _LoadSessionBackend:
    """支持会话恢复的最小 Backend，用于验证能力声明路径。"""

    agent_name = "测试 Agent"
    agent_version = "1.0.0"
    supports_load_session = True

    async def create_session(
        self,
        session_id: str,
        cwd: str,
        _mcp_servers: list[dict[str, Any]],
    ) -> AcpSession:
        return AcpSession(session_id=session_id, cwd=cwd)

    async def load_session(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[dict[str, Any]],
    ) -> AcpSession:
        return await self.create_session(session_id, cwd, mcp_servers)

    async def prompt(
        self,
        _session: AcpSession,
        _text: str,
        _emit: Any,
        _request_permission: Any,
    ) -> dict[str, Any]:
        return {"stopReason": "end_turn"}

    async def cancel(self, _session: AcpSession) -> None:
        return None


class _BlockingPermissionBackend:
    """prompt 执行中阻塞等待权限询问的 Backend，用于验证取消清理。"""

    agent_name = "审批阻塞 Agent"
    agent_version = "1.0.0"
    supports_load_session = False

    async def create_session(
        self,
        session_id: str,
        cwd: str,
        _mcp_servers: list[dict[str, Any]],
    ) -> AcpSession:
        return AcpSession(session_id=session_id, cwd=cwd)

    async def load_session(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[dict[str, Any]],
    ) -> AcpSession:
        return await self.create_session(session_id, cwd, mcp_servers)

    async def prompt(
        self,
        _session: AcpSession,
        _text: str,
        emit: Any,
        request_permission: Any,
    ) -> dict[str, Any]:
        await emit(AcpUpdate.thought("等待人工审批"))
        return await request_permission(
            {
                "toolCall": {"toolCallId": "tool-1"},
                "options": [
                    {"optionId": "approve_once", "name": "批准", "kind": "allow_once"},
                    {"optionId": "reject_once", "name": "拒绝", "kind": "reject_once"},
                ],
            }
        )

    async def cancel(self, _session: AcpSession) -> None:
        return None


class _ReplayBackend(_LoadSessionBackend):
    """声明会话恢复并回放历史消息的 Backend。"""

    async def replay_session(self, _session: AcpSession, emit: Any) -> None:
        await emit(AcpUpdate.user_text("历史问题"))
        await emit(AcpUpdate.text("历史回答"))


def _make_server(backend: Any) -> tuple[AcpStdioServer, io.StringIO]:
    stdout = io.StringIO()
    server = AcpStdioServer(backend, stdin=io.StringIO(), stdout=stdout, approval_timeout_seconds=60)
    return server, stdout


async def _wait_for_pending(server: AcpStdioServer) -> None:
    for _ in range(200):
        if server._pending_client_requests:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("权限询问未进入 pending 队列")


async def test_initialize_reports_configured_capabilities() -> None:
    """Backend 声明 capabilities 时，initialize 应逐项透出。"""
    backend = _LoadSessionBackend()
    backend.capabilities = AcpCapabilities(  # type: ignore[attr-defined]
        load_session=True,
        image=True,
        audio=True,
        embedded_context=True,
        mcp_http=True,
        mcp_sse=True,
    )
    server, _ = _make_server(backend)
    result = server._initialize({"protocolVersion": 1})
    assert result["agentCapabilities"] == {
        "loadSession": True,
        "promptCapabilities": {"image": True, "audio": True, "embeddedContext": True},
        "mcpCapabilities": {"http": True, "sse": True},
    }


async def test_initialize_falls_back_to_supports_load_session() -> None:
    """未声明 capabilities 时回退 supports_load_session，其余能力保持关闭。"""
    backend = _LoadSessionBackend()
    server, _ = _make_server(backend)
    result = server._initialize({"protocolVersion": 1})
    assert result["agentCapabilities"] == {
        "loadSession": True,
        "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
        "mcpCapabilities": {"http": False, "sse": False},
    }


async def test_session_load_respects_capabilities() -> None:
    """capabilities.load_session 优先于旧标记控制 session/load 可用性。"""
    backend = _LoadSessionBackend()
    backend.capabilities = AcpCapabilities(load_session=False)  # type: ignore[attr-defined]
    server, _ = _make_server(backend)
    try:
        await server._dispatch("session/load", {"sessionId": "s1", "cwd": "."})
    except NotImplementedError:
        pass
    else:
        raise AssertionError("load_session=False 时 session/load 应不可用")

    backend.capabilities = AcpCapabilities(load_session=True)  # type: ignore[attr-defined]
    result = await server._dispatch("session/load", {"sessionId": "s1", "cwd": "."})
    assert result["sessionId"] == "s1"
    assert isinstance(server._session("s1"), AcpSession)


async def test_session_load_without_capability_raises_not_implemented() -> None:
    """未声明 capabilities 且 supports_load_session=False 时，session/load 不可用。"""
    server, _ = _make_server(_BlockingPermissionBackend())
    try:
        await server._dispatch("session/load", {"sessionId": "s1", "cwd": "."})
    except NotImplementedError:
        pass
    else:
        raise AssertionError("不支持会话恢复时应抛出 NotImplementedError")


async def test_session_load_replays_history_updates() -> None:
    """支持恢复的 Backend 应在返回前回放历史会话更新。"""
    server, stdout = _make_server(_ReplayBackend())
    result = await server._dispatch("session/load", {"sessionId": "s1", "cwd": "."})

    assert result["sessionId"] == "s1"
    updates = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [item["params"]["update"]["sessionUpdate"] for item in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
    ]
    assert updates[0]["params"]["sessionId"] == "s1"


async def test_permission_accepts_nested_selected_outcome() -> None:
    """ACP Client 返回嵌套 outcome 时应正确识别选中的审批选项。"""
    server, _ = _make_server(_LoadSessionBackend())

    async def fake_request_client(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"outcome": {"outcome": "selected", "optionId": "approve_once"}}

    server._request_client = fake_request_client  # type: ignore[method-assign]
    result = await server._request_permission(
        "s1",
        {
            "toolCall": {"toolCallId": "tool-1"},
            "options": [
                {"optionId": "approve_once", "name": "批准", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "拒绝", "kind": "reject_once"},
            ],
        },
    )
    assert result == "approve_once"


async def test_permission_rejects_nested_cancelled_outcome() -> None:
    """嵌套 cancelled outcome 应按拒绝处理。"""
    server, _ = _make_server(_LoadSessionBackend())

    async def fake_request_client(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"outcome": {"outcome": "cancelled"}}

    server._request_client = fake_request_client  # type: ignore[method-assign]
    result = await server._request_permission(
        "s1",
        {
            "toolCall": {"toolCallId": "tool-1"},
            "options": [
                {"optionId": "approve_once", "name": "批准", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "拒绝", "kind": "reject_once"},
            ],
        },
    )
    assert result == "reject_once"


async def test_cancel_clears_pending_permission_requests() -> None:
    """取消会话应立即终结挂起的权限询问，而不是等到审批超时。"""
    backend = _BlockingPermissionBackend()
    server, stdout = _make_server(backend)
    created = await server._new_session({"cwd": "."})
    session_id = created["sessionId"]

    prompt_task = asyncio.create_task(
        server._handle_request(
            "req-prompt-1",
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "执行写操作"}]},
        )
    )
    await _wait_for_pending(server)
    pending_id = next(iter(server._pending_client_requests))

    # 审批超时设置为 60s：若取消未清理 pending，该断言会超时失败。
    await asyncio.wait_for(
        server._handle_request(None, "session/cancel", {"sessionId": session_id}),
        timeout=2,
    )
    await asyncio.wait_for(prompt_task, timeout=2)

    # pending 权限请求应被立即移除。
    assert pending_id not in server._pending_client_requests
    assert server._pending_client_requests == {}
    # prompt 请求应以 cancelled 语义结束。
    assert '"stopReason":"cancelled"' in stdout.getvalue()


async def test_cancel_only_clears_own_session_requests() -> None:
    """取消 A 会话不应影响 B 会话仍在等待的权限询问。"""
    backend = _BlockingPermissionBackend()
    server, _ = _make_server(backend)
    session_a = (await server._new_session({"cwd": "."}))["sessionId"]
    session_b = (await server._new_session({"cwd": "."}))["sessionId"]

    task_a = asyncio.create_task(
        server._handle_request(
            "req-a", "session/prompt", {"sessionId": session_a, "prompt": [{"type": "text", "text": "a"}]}
        )
    )
    task_b = asyncio.create_task(
        server._handle_request(
            "req-b", "session/prompt", {"sessionId": session_b, "prompt": [{"type": "text", "text": "b"}]}
        )
    )
    await _wait_for_pending(server)
    # 等第二个会话的权限询问也进入 pending 队列。
    for _ in range(200):
        if len(server._pending_client_requests) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(server._pending_client_requests) == 2

    await asyncio.wait_for(
        server._handle_request(None, "session/cancel", {"sessionId": session_a}), timeout=2
    )
    await asyncio.wait_for(task_a, timeout=2)
    assert len(server._pending_client_requests) == 1
    remaining_session = next(iter(server._pending_client_requests.values()))[0]
    assert remaining_session == session_b

    # 清理 B 会话，避免测试遗留悬挂任务。
    await asyncio.wait_for(
        server._handle_request(None, "session/cancel", {"sessionId": session_b}), timeout=2
    )
    await asyncio.wait_for(task_b, timeout=2)
    assert server._pending_client_requests == {}


async def test_cancel_unknown_session_raises() -> None:
    """取消不存在的会话应返回会话不存在错误语义。"""
    server, _ = _make_server(_BlockingPermissionBackend())
    try:
        await server._cancel({"sessionId": "missing"})
    except AcpSessionNotFoundError:
        pass
    else:
        raise AssertionError("取消不存在的会话应抛出 AcpSessionNotFoundError")
