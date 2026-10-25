# -*- coding: utf-8 -*-
"""McpToolClient 重连生命周期回调测试（on_reconnected / on_give_up）。"""

from __future__ import annotations

import unittest

from agent_core.mcp.client import McpConnectionError, McpToolClient


class TestMcpReconnectCallbacks(unittest.IsolatedAsyncioTestCase):
    async def test_on_reconnected_fires_after_successful_reconnect(self) -> None:
        """会话断开后重连成功，应触发 on_reconnected 并重置放弃通知标记。"""
        events: list[str] = []

        async def on_reconnected() -> None:
            events.append("reconnected")

        async def on_give_up(error: Exception) -> None:
            events.append(f"give_up:{error}")

        client = McpToolClient(
            transport="http",
            url="http://test",
            max_reconnects=2,
            reconnect_delay_seconds=0,
            on_reconnected=on_reconnected,
            on_give_up=on_give_up,
        )

        async def fake_start() -> None:
            client._session = object()  # 模拟会话就绪

        client.start = fake_start  # type: ignore[method-assign]

        # 第一次调用：首次建连成功（不算重连）。
        await client._require_session()
        self.assertEqual(events, [])

        # 模拟会话断开：进入重连分支并成功。
        client._session = None
        client._terminal_error = McpConnectionError("连接断了")
        await client._require_session()
        self.assertEqual(events, ["reconnected"])
        self.assertFalse(client._give_up_notified)

    async def test_on_give_up_fires_once_when_budget_exhausted(self) -> None:
        """重连预算耗尽后，on_give_up 只触发一次，后续调用不再重复通知。"""
        give_up_errors: list[Exception] = []

        async def on_give_up(error: Exception) -> None:
            give_up_errors.append(error)

        client = McpToolClient(
            transport="http",
            url="http://test",
            max_reconnects=0,  # 0 = 不重连，直接进入放弃分支
            on_give_up=on_give_up,
        )

        async def failing_start() -> None:
            client._session = None
            client._terminal_error = McpConnectionError("服务仍然不可用")

        client.start = failing_start  # type: ignore[method-assign]
        client._terminal_error = McpConnectionError("首次断开")

        with self.assertRaises(McpConnectionError):
            await client._require_session()
        self.assertEqual(len(give_up_errors), 1)

        with self.assertRaises(McpConnectionError):
            await client._require_session()
        # 预算耗尽后只通知一次，避免每次调用重复触发。
        self.assertEqual(len(give_up_errors), 1)


if __name__ == "__main__":
    unittest.main()
