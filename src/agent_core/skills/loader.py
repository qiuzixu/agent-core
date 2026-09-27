"""从受约束的 JSON 清单加载 Skill。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from agent_core.skills.errors import SkillLoadError
from agent_core.skills.types import SkillSpec, SkillToolKind, SkillToolSpec

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SKILL_FIELDS = {
    "name",
    "description",
    "version",
    "instructions",
    "instructions_file",
    "tools",
    "metadata",
}
_TOOL_FIELDS = {
    "name",
    "kind",
    "target",
    "server",
    "description",
    "parameters",
    "risk_level",
    "requires_approval",
    "timeout_seconds",
    "idempotent",
}


class SkillLoader:
    """加载 ``skill.json``，并校验其关联指令文件位于 Skill 目录内。"""

    def __init__(self, *, manifest_name: str = "skill.json") -> None:
        if not manifest_name or Path(manifest_name).name != manifest_name:
            raise ValueError("manifest_name 必须是单个文件名")
        self._manifest_name = manifest_name

    def load(self, path: str | Path) -> SkillSpec:
        """从 Skill 目录或 JSON 清单文件加载一个 Skill。"""
        source = Path(path).expanduser()
        manifest = source / self._manifest_name if source.is_dir() else source
        manifest = manifest.resolve()
        if not manifest.is_file():
            raise SkillLoadError(f"Skill 清单不存在：{manifest}")

        data = self._read_manifest(manifest)
        self._reject_unknown_fields(data, _SKILL_FIELDS, f"Skill {manifest}")

        name = self._required_string(data, "name", f"Skill {manifest}")
        if not _NAME_PATTERN.fullmatch(name):
            raise SkillLoadError(f"Skill 名称格式无效：{name!r}；只允许字母、数字、点、下划线和连字符")
        description = self._required_string(data, "description", f"Skill {name}")
        version = self._required_string(data, "version", f"Skill {name}")
        instructions = self._load_instructions(data, manifest, name)

        raw_tools = data.get("tools", [])
        if not isinstance(raw_tools, list):
            raise SkillLoadError(f"Skill {name} 的 tools 必须是数组")
        tools = tuple(
            self._parse_tool(item, skill_name=name, index=index) for index, item in enumerate(raw_tools)
        )
        duplicate_tools = self._duplicates(tool.name for tool in tools)
        if duplicate_tools:
            raise SkillLoadError(f"Skill {name} 包含重复工具名称：{', '.join(duplicate_tools)}")

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SkillLoadError(f"Skill {name} 的 metadata 必须是对象")

        return SkillSpec(
            name=name,
            description=description,
            version=version,
            instructions=instructions,
            tools=tools,
            metadata=dict(metadata),
            source=manifest,
        )

    def load_directory(self, directory: str | Path) -> tuple[SkillSpec, ...]:
        """递归加载目录下的所有 ``skill.json``，结果按路径稳定排序。"""
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise SkillLoadError(f"Skill 目录不存在：{root}")
        manifests = sorted(root.rglob(self._manifest_name))
        if not manifests:
            raise SkillLoadError(f"目录中没有找到 {self._manifest_name}：{root}")
        return tuple(self.load(manifest) for manifest in manifests)

    @staticmethod
    def _read_manifest(manifest: Path) -> dict[str, Any]:
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SkillLoadError(f"无法读取 Skill 清单：{manifest}（{exc}）") from exc
        except json.JSONDecodeError as exc:
            raise SkillLoadError(f"Skill 清单不是有效 JSON：{manifest}:{exc.lineno}:{exc.colno}") from exc
        if not isinstance(raw, dict):
            raise SkillLoadError(f"Skill 清单根节点必须是对象：{manifest}")
        return cast(dict[str, Any], raw)

    def _load_instructions(
        self,
        data: dict[str, Any],
        manifest: Path,
        skill_name: str,
    ) -> str:
        inline = data.get("instructions")
        filename = data.get("instructions_file")
        if inline is not None and filename is not None:
            raise SkillLoadError(f"Skill {skill_name} 不能同时设置 instructions 和 instructions_file")
        if inline is not None:
            if not isinstance(inline, str):
                raise SkillLoadError(f"Skill {skill_name} 的 instructions 必须是字符串")
            return inline.strip()
        if filename is None:
            return ""
        if not isinstance(filename, str) or not filename.strip():
            raise SkillLoadError(f"Skill {skill_name} 的 instructions_file 必须是非空字符串")

        skill_root = manifest.parent.resolve()
        instruction_path = (skill_root / filename).resolve()
        try:
            instruction_path.relative_to(skill_root)
        except ValueError as exc:
            raise SkillLoadError(f"Skill {skill_name} 的 instructions_file 不能越过 Skill 目录") from exc
        if not instruction_path.is_file():
            raise SkillLoadError(f"Skill {skill_name} 的指令文件不存在：{instruction_path}")
        try:
            return instruction_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SkillLoadError(
                f"无法读取 Skill {skill_name} 的指令文件：{instruction_path}（{exc}）"
            ) from exc

    def _parse_tool(
        self,
        raw: object,
        *,
        skill_name: str,
        index: int,
    ) -> SkillToolSpec:
        context = f"Skill {skill_name} 的 tools[{index}]"
        if not isinstance(raw, dict):
            raise SkillLoadError(f"{context} 必须是对象")
        data = cast(dict[str, Any], raw)
        self._reject_unknown_fields(data, _TOOL_FIELDS, context)

        name = self._required_string(data, "name", context)
        if not _TOOL_NAME_PATTERN.fullmatch(name):
            raise SkillLoadError(
                f"{context} 的工具名称格式无效：{name!r}；必须符合 Function Calling 命名规则"
            )
        kind = self._required_string(data, "kind", context).lower()
        if kind not in {"function", "mcp"}:
            raise SkillLoadError(f"{context} 的 kind 只支持 function 或 mcp")
        description = self._required_string(data, "description", context)
        target = data.get("target", name)
        if not isinstance(target, str) or not target.strip():
            raise SkillLoadError(f"{context} 的 target 必须是非空字符串")
        target = target.strip()

        server = data.get("server")
        if kind == "mcp":
            if not isinstance(server, str) or not server.strip():
                raise SkillLoadError(f"{context} 的 MCP 工具必须声明 server")
            server = server.strip()
        elif server is not None:
            raise SkillLoadError(f"{context} 的 Function 工具不能声明 server")

        parameters = data.get(
            "parameters",
            {"type": "object", "properties": {}, "required": []},
        )
        self._validate_parameters(parameters, context)

        risk_level = data.get("risk_level", "read")
        if not isinstance(risk_level, str) or not risk_level.strip():
            raise SkillLoadError(f"{context} 的 risk_level 必须是非空字符串")
        requires_approval = self._boolean(data, "requires_approval", False, context)
        idempotent = self._boolean(data, "idempotent", True, context)
        timeout_seconds = data.get("timeout_seconds")
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or timeout_seconds <= 0
            ):
                raise SkillLoadError(f"{context} 的 timeout_seconds 必须大于 0")
            timeout_seconds = float(timeout_seconds)

        return SkillToolSpec(
            name=name,
            kind=cast(SkillToolKind, kind),
            target=target,
            server=server,
            description=description,
            parameters=dict(cast(dict[str, Any], parameters)),
            risk_level=risk_level.strip(),
            requires_approval=requires_approval,
            timeout_seconds=timeout_seconds,
            idempotent=idempotent,
        )

    @staticmethod
    def _validate_parameters(parameters: object, context: str) -> None:
        if not isinstance(parameters, dict):
            raise SkillLoadError(f"{context} 的 parameters 必须是 JSON Schema 对象")
        if parameters.get("type", "object") != "object":
            raise SkillLoadError(f"{context} 的 parameters.type 必须是 object")
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        if not isinstance(properties, dict):
            raise SkillLoadError(f"{context} 的 parameters.properties 必须是对象")
        if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
            raise SkillLoadError(f"{context} 的 parameters.required 必须是字符串数组")

    @staticmethod
    def _required_string(data: dict[str, Any], field: str, context: str) -> str:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SkillLoadError(f"{context} 缺少非空字符串字段 {field}")
        return value.strip()

    @staticmethod
    def _boolean(data: dict[str, Any], field: str, default: bool, context: str) -> bool:
        value = data.get(field, default)
        if not isinstance(value, bool):
            raise SkillLoadError(f"{context} 的 {field} 必须是布尔值")
        return value

    @staticmethod
    def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], context: str) -> None:
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise SkillLoadError(f"{context} 包含未知字段：{', '.join(unknown)}")

    @staticmethod
    def _duplicates(values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            if value in seen:
                duplicates.add(value)
            seen.add(value)
        return sorted(duplicates)


__all__ = ["SkillLoader"]
