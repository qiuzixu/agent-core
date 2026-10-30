"""Skill 注册、选择和工具绑定。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from agent_core.mcp import McpToolCaller
from agent_core.skills.errors import SkillBindingError, SkillRegistrationError
from agent_core.skills.loader import SkillLoader
from agent_core.skills.types import SkillActivation, SkillSpec, SkillToolSpec
from agent_core.tools import ToolRegistry

logger = logging.getLogger(__name__)


class SkillRegistry:
    """管理 Skill，并把激活 Skill 的工具安装到统一工具注册表。"""

    def __init__(self) -> None:
        self._skills: dict[str, SkillSpec] = {}
        # 记录每个 Skill 清单文件的 mtime，供 check_for_changes / reload_changed 使用。
        self._mtimes: dict[str, float] = {}

    def register(self, skill: SkillSpec, *, replace: bool = False) -> None:
        """注册一个 Skill；默认拒绝无意覆盖同名版本。"""
        if skill.name in self._skills and not replace:
            raise SkillRegistrationError(f"Skill 已注册：{skill.name}")
        self._skills[skill.name] = skill
        self._refresh_mtime(skill.name)

    def unregister(self, name: str) -> None:
        """移除一个 Skill。"""
        self._skills.pop(name, None)
        self._mtimes.pop(name, None)

    def get(self, name: str) -> SkillSpec | None:
        """按名称查询 Skill。"""
        return self._skills.get(name)

    def names(self) -> tuple[str, ...]:
        """按名称排序返回已注册 Skill。"""
        return tuple(sorted(self._skills))

    def all(self) -> tuple[SkillSpec, ...]:
        """返回全部 Skill 规格。"""
        return tuple(self._skills[name] for name in self.names())

    def load(
        self,
        path: str | Path,
        *,
        loader: SkillLoader | None = None,
        replace: bool = False,
    ) -> SkillSpec:
        """加载并注册一个 Skill。"""
        skill = (loader or SkillLoader()).load(path)
        self.register(skill, replace=replace)
        return skill

    def load_directory(
        self,
        directory: str | Path,
        *,
        loader: SkillLoader | None = None,
        replace: bool = False,
    ) -> tuple[SkillSpec, ...]:
        """批量加载 Skill，并在写入注册表前完成全部重名检查。"""
        skills = (loader or SkillLoader()).load_directory(directory)
        batch_duplicates = self._duplicates(skill.name for skill in skills)
        if batch_duplicates:
            raise SkillRegistrationError(f"目录中包含重复 Skill：{', '.join(batch_duplicates)}")
        conflicts = sorted(skill.name for skill in skills if skill.name in self._skills and not replace)
        if conflicts:
            raise SkillRegistrationError(f"Skill 已注册：{', '.join(conflicts)}")
        for skill in skills:
            self.register(skill, replace=replace)
        return skills

    def activate(
        self,
        names: str | Iterable[str] | None = None,
        *,
        tool_registry: ToolRegistry,
        functions: Mapping[str, Callable[..., Any]] | None = None,
        mcp_clients: Mapping[str, McpToolCaller] | None = None,
        replace_tools: bool = False,
    ) -> SkillActivation:
        """激活 Skill，并绑定其 Function 与 MCP 工具。

        ``functions`` 的键对应工具 ``target``；``mcp_clients`` 的键对应 MCP
        工具 ``server``。清单只声明绑定关系，不会动态导入任意 Python 代码。
        """
        selected = self._select(names)
        functions = functions or {}
        mcp_clients = mcp_clients or {}

        declared: dict[str, tuple[str, SkillToolSpec]] = {}
        bindings: list[tuple[SkillToolSpec, Callable[..., Any]]] = []
        for skill in selected:
            for tool in skill.tools:
                previous = declared.get(tool.name)
                if previous is not None:
                    raise SkillRegistrationError(
                        f"Skill {skill.name} 与 {previous[0]} 重复声明工具：{tool.name}"
                    )
                declared[tool.name] = (skill.name, tool)
                if tool_registry.get_tool(tool.name) is not None and not replace_tools:
                    raise SkillRegistrationError(f"工具已注册：{tool.name}")
                bindings.append(
                    (
                        tool,
                        self._resolve_binding(
                            tool,
                            skill_name=skill.name,
                            functions=functions,
                            mcp_clients=mcp_clients,
                        ),
                    )
                )

        # 所有依赖和冲突都通过后再写注册表，保证失败不会产生部分注册。
        for tool, function in bindings:
            tool_registry.register(
                tool.name,
                function,
                tool.description,
                parameters=tool.parameters,
                risk_level=tool.risk_level,
                requires_approval=tool.requires_approval,
                timeout_seconds=tool.timeout_seconds,
                idempotent=tool.idempotent,
                replace=replace_tools,
            )

        instructions = "\n\n".join(
            skill.instructions.strip() for skill in selected if skill.instructions.strip()
        )
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for _, tool in declared.values()
        ]
        return SkillActivation(
            skills=selected,
            instructions=instructions,
            tool_names=tuple(declared),
            tool_definitions=definitions,
        )

    def check_for_changes(self) -> list[str]:
        """检查已注册 Skill 的源文件是否发生变化。

        返回需要重新加载的 Skill 名称列表。首次检查只建立基线，不把刚注册的
        Skill 当作变更；后续通过比较 ``skill.json`` 的 mtime 发现修改。
        """
        changed: list[str] = []
        for name, skill in self._skills.items():
            if skill.source is None or not skill.source.is_file():
                continue
            current_mtime = skill.source.stat().st_mtime
            cached = self._mtimes.get(name)
            if cached is not None and cached != current_mtime:
                # 变更基线只在成功 reload 后刷新，避免调用方先 check 再 reload 时丢失变更。
                changed.append(name)
            else:
                self._mtimes[name] = current_mtime
        return changed

    def reload_changed(self) -> list[str]:
        """重新加载源文件发生变化的 Skill。

        旧实现先从注册表移除旧 Skill 再加载新文件；如果清单损坏，Skill 会被
        静默吞掉。现在采用“先加载候选版本，成功后再替换”的顺序，加载失败时
        保留旧版本并输出警告日志。
        """
        changed = self.check_for_changes()
        reloaded: list[str] = []
        for name in changed:
            skill = self._skills.get(name)
            if skill is None or skill.source is None:
                continue
            try:
                # 先生成完整候选对象；加载失败时不会影响注册表中的旧版本。
                candidate = SkillLoader().load(skill.source)
                self.register(candidate, replace=True)
                reloaded.append(name)
                logger.info(
                    "Skill 热重载成功：%s（%s -> %s）",
                    name,
                    skill.version,
                    candidate.version,
                )
            except Exception as exc:
                logger.warning("Skill 热重载失败，保留旧版本：%s（%s）", name, exc)
        return reloaded

    def _snapshot_mtime(self, name: str, skill: SkillSpec) -> float:
        """返回 Skill 清单 mtime；源文件不存在时记为 0。"""
        if skill.source is None or not skill.source.is_file():
            return 0.0
        return skill.source.stat().st_mtime

    def _refresh_mtime(self, name: str) -> None:
        """注册或移除 Skill 后刷新 mtime 基线。"""
        skill = self._skills.get(name)
        if skill is None:
            self._mtimes.pop(name, None)
            return
        self._mtimes[name] = self._snapshot_mtime(name, skill)

    def _select(self, names: str | Iterable[str] | None) -> tuple[SkillSpec, ...]:
        if names is None:
            selected_names = list(self.names())
        elif isinstance(names, str):
            selected_names = [names]
        else:
            selected_names = list(names)
        duplicates = self._duplicates(selected_names)
        if duplicates:
            raise SkillRegistrationError(f"重复激活 Skill：{', '.join(duplicates)}")
        missing = [name for name in selected_names if name not in self._skills]
        if missing:
            raise SkillRegistrationError(f"Skill 未注册：{', '.join(missing)}")
        return tuple(self._skills[name] for name in selected_names)

    @staticmethod
    def _resolve_binding(
        tool: SkillToolSpec,
        *,
        skill_name: str,
        functions: Mapping[str, Callable[..., Any]],
        mcp_clients: Mapping[str, McpToolCaller],
    ) -> Callable[..., Any]:
        if tool.kind == "function":
            function = functions.get(tool.target)
            if function is None:
                raise SkillBindingError(f"Skill {skill_name} 缺少 Function 绑定：{tool.target}")
            if not callable(function):
                raise SkillBindingError(f"Skill {skill_name} 的 Function 绑定不可调用：{tool.target}")
            return function

        server = tool.server
        if server is None:
            raise SkillBindingError(f"Skill {skill_name} 的 MCP 工具未声明 server")
        client = mcp_clients.get(server)
        if client is None:
            raise SkillBindingError(f"Skill {skill_name} 缺少 MCP 客户端：{server}")

        async def call_mcp(**arguments: Any) -> dict[str, object]:
            return await client.call_tool(tool.target, arguments)

        return call_mcp

    @staticmethod
    def _duplicates(values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            if value in seen:
                duplicates.add(value)
            seen.add(value)
        return sorted(duplicates)


__all__ = ["SkillRegistry"]
