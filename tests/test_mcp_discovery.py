"""MCP 工具自动发现注册测试（描述覆盖 / 参数覆盖 / 基础注册流程）。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from agent_core.mcp.client import JsonObject, McpToolCaller
from agent_core.tools.config import McpDiscoveryConfig
from agent_core.tools.discovery import auto_register_mcp_tools
from agent_core.tools.executor import ToolRegistry


class FakeMcpClient(McpToolCaller):
    """最小 MCP 客户端桩：返回固定工具定义，调用返回回显。"""

    def __init__(self, definitions: list[JsonObject]) -> None:
        self._definitions = definitions

    async def list_tools(self) -> list[str]:
        return [str(tool.get("name", "")) for tool in self._definitions]

    async def list_tool_definitions(self) -> list[JsonObject]:
        return self._definitions

    async def call_tool(
        self, name: str, arguments: dict[str, object] | None = None
    ) -> JsonObject:
        return {"tool": name, "arguments": dict(arguments or {})}


def _client() -> FakeMcpClient:
    return FakeMcpClient(
        [
            {
                "name": "geocode",
                "description": "Geocoding service with awkward English.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
            {"name": "plain_tool", "description": "原样直传的描述", "inputSchema": {}},
        ]
    )


class TestAutoRegisterMcpTools(unittest.IsolatedAsyncioTestCase):
    async def test_description_passthrough_by_default(self) -> None:
        """无覆盖时，server 返回的 description 原样进入注册表。"""
        registry = ToolRegistry()
        registered = await auto_register_mcp_tools(_client(), registry)
        self.assertEqual(set(registered), {"geocode", "plain_tool"})
        spec = registry.get_spec("plain_tool")
        assert spec is not None
        self.assertEqual(spec.description, "原样直传的描述")

    async def test_description_override_wins(self) -> None:
        """descriptions_override 中的描述应覆盖 server 返回值，其余不动。"""
        registry = ToolRegistry()
        await auto_register_mcp_tools(
            _client(),
            registry,
            descriptions_override={"geocode": "地名转坐标，输入城市中文名"},
        )
        spec = registry.get_spec("geocode")
        assert spec is not None
        self.assertEqual(spec.description, "地名转坐标，输入城市中文名")
        # 未覆盖的工具保持原样。
        other = registry.get_spec("plain_tool")
        assert other is not None
        self.assertEqual(other.description, "原样直传的描述")
        # 工具定义（发给模型的菜单）也使用覆盖后的描述。
        definitions = {d["function"]["name"]: d for d in registry.build_tool_definitions()}
        geocode = definitions["geocode"]["function"]
        assert isinstance(geocode, dict)
        self.assertEqual(geocode["description"], "地名转坐标，输入城市中文名")

    async def test_config_from_env_parses_descriptions_json(self) -> None:
        """环境变量 JSON 应解析为 descriptions_override；非法 JSON 静默回退。"""
        env = {
            "AGENT_MCP_DISCOVERY_MODE": "auto",
            "AGENT_MCP_DISCOVERY_DESCRIPTIONS": '{"geocode": "中文描述"}',
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = McpDiscoveryConfig.from_env()
        self.assertEqual(cfg.descriptions_override, {"geocode": "中文描述"})

        with patch.dict(
            os.environ, {"AGENT_MCP_DISCOVERY_DESCRIPTIONS": "not-json"}, clear=False
        ):
            cfg = McpDiscoveryConfig.from_env()
        self.assertIsNone(cfg.descriptions_override)


if __name__ == "__main__":
    unittest.main()
