"""Skill 加载、激活和工具绑定回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from agent_core import (
    SkillBindingError,
    SkillLoader,
    SkillLoadError,
    SkillRegistrationError,
    SkillRegistry,
    ToolExecutor,
    ToolRegistry,
)


def _write_skill(
    directory: Path,
    *,
    name: str,
    tools: list[dict[str, object]],
    instructions: str = "按 Skill 指令执行。",
) -> Path:
    """写入一个最小可加载的 Skill 目录。"""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(instructions, encoding="utf-8")
    manifest = {
        "name": name,
        "description": f"{name} 测试 Skill",
        "version": "1.0.0",
        "instructions_file": "SKILL.md",
        "tools": tools,
        "metadata": {"owner": "test"},
    }
    path = directory / "skill.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


class _FakeMcpClient:
    """记录远端工具名和参数的最小 MCP 调用端。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def list_tools(self) -> list[str]:
        return ["entity_list"]

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        payload = dict(arguments or {})
        self.calls.append((name, payload))
        return {"remote": name, "arguments": payload}


class TestSkillSystem(unittest.IsolatedAsyncioTestCase):
    async def test_load_and_activate_function_skill(self) -> None:
        """清单指令和 Function 绑定可直接用于 ToolExecutor。"""
        with tempfile.TemporaryDirectory() as directory:
            path = _write_skill(
                Path(directory) / "weather",
                name="weather",
                tools=[
                    {
                        "name": "lookup_weather",
                        "kind": "function",
                        "target": "weather_lookup",
                        "description": "查询天气",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    }
                ],
            )
            skills = SkillRegistry()
            skill = skills.load(path)
            tools = ToolRegistry()
            activation = skills.activate(
                "weather",
                tool_registry=tools,
                functions={"weather_lookup": lambda city: f"{city}:sunny"},
            )

            result = await ToolExecutor(tools).execute_result("lookup_weather", {"city": "北京"})

            self.assertEqual(skill.instructions, "按 Skill 指令执行。")
            self.assertEqual(
                activation.compose_system_prompt("基础提示词"), "基础提示词\n\n按 Skill 指令执行。"
            )
            self.assertEqual(activation.tool_names, ("lookup_weather",))
            self.assertEqual(
                activation.tool_definitions[0]["function"]["name"],
                "lookup_weather",
            )
            self.assertTrue(result.success)
            self.assertEqual(result.value, "北京:sunny")

    async def test_activate_mcp_skill_calls_declared_server_and_target(self) -> None:
        """MCP Skill 使用 server 选择客户端，并调用 target 指定的远端工具。"""
        with tempfile.TemporaryDirectory() as directory:
            _write_skill(
                Path(directory) / "cesium",
                name="cesium-scene",
                tools=[
                    {
                        "name": "list_entities",
                        "kind": "mcp",
                        "server": "cesium",
                        "target": "entity_list",
                        "description": "列出实体",
                        "parameters": {
                            "type": "object",
                            "properties": {"layer": {"type": "string"}},
                        },
                    }
                ],
            )
            skills = SkillRegistry()
            skills.load_directory(directory)
            tools = ToolRegistry()
            client = _FakeMcpClient()
            skills.activate(
                tool_registry=tools,
                mcp_clients={"cesium": client},
            )

            result = await ToolExecutor(tools).execute_result("list_entities", {"layer": "route"})

            self.assertTrue(result.success)
            self.assertEqual(client.calls, [("entity_list", {"layer": "route"})])
            self.assertEqual(result.value, {"remote": "entity_list", "arguments": {"layer": "route"}})

    def test_activation_returns_only_selected_skill_tools(self) -> None:
        """按需激活不会把未选择 Skill 或原注册表工具暴露给模型。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("alpha", "beta"):
                _write_skill(
                    root / name,
                    name=name,
                    tools=[
                        {
                            "name": f"{name}_tool",
                            "kind": "function",
                            "target": f"{name}_function",
                            "description": f"{name} 工具",
                        }
                    ],
                )
            skills = SkillRegistry()
            loaded = skills.load_directory(root)
            tools = ToolRegistry()
            tools.register("existing", lambda: "existing", "已有工具")

            activation = skills.activate(
                ["beta"],
                tool_registry=tools,
                functions={"beta_function": lambda: "beta"},
            )

            self.assertEqual(tuple(skill.name for skill in loaded), ("alpha", "beta"))
            self.assertEqual(activation.tool_names, ("beta_tool",))
            self.assertEqual(
                [item["function"]["name"] for item in activation.tool_definitions],
                ["beta_tool"],
            )
            self.assertEqual(tools.get_tool_names(), ["existing", "beta_tool"])

    def test_loader_rejects_invalid_manifest_and_path_escape(self) -> None:
        """清单缺字段、未知字段和越界指令路径都会在加载阶段失败。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = root / "invalid"
            skill_dir.mkdir()
            manifest = skill_dir / "skill.json"

            invalid_manifests = [
                {"description": "缺少名称", "version": "1.0.0"},
                {
                    "name": "invalid",
                    "description": "未知字段",
                    "version": "1.0.0",
                    "unexpected": True,
                },
                {
                    "name": "invalid",
                    "description": "路径越界",
                    "version": "1.0.0",
                    "instructions_file": "../outside.md",
                },
            ]
            (root / "outside.md").write_text("不应读取", encoding="utf-8")
            for data in invalid_manifests:
                with self.subTest(data=data):
                    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    with self.assertRaises(SkillLoadError):
                        SkillLoader().load(skill_dir)

    def test_registry_rejects_duplicates_and_binding_failure_is_atomic(self) -> None:
        """重名或缺少绑定时，不向 ToolRegistry 写入部分工具。"""
        with tempfile.TemporaryDirectory() as directory:
            path = _write_skill(
                Path(directory) / "atomic",
                name="atomic",
                tools=[
                    {
                        "name": "available",
                        "kind": "function",
                        "target": "available_function",
                        "description": "可绑定工具",
                    },
                    {
                        "name": "missing",
                        "kind": "function",
                        "target": "missing_function",
                        "description": "缺少绑定的工具",
                    },
                ],
            )
            skills = SkillRegistry()
            skill = skills.load(path)
            with self.assertRaises(SkillRegistrationError):
                skills.register(skill)

            tools = ToolRegistry()
            with self.assertRaises(SkillBindingError):
                skills.activate(
                    tool_registry=tools,
                    functions={"available_function": lambda: "ok"},
                )
            self.assertEqual(tools.get_tool_names(), [])


if __name__ == "__main__":
    unittest.main()
