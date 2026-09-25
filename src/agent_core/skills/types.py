"""Skill 公共数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SkillToolKind = Literal["function", "mcp"]


@dataclass(frozen=True, slots=True)
class SkillToolSpec:
    """Skill 声明的一个可调用工具。"""

    name: str
    kind: SkillToolKind
    target: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    server: str | None = None
    risk_level: str = "read"
    requires_approval: bool = False
    timeout_seconds: float | None = None
    idempotent: bool = True


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """一个可复用 Skill 的完整声明。"""

    name: str
    description: str
    version: str
    instructions: str = ""
    tools: tuple[SkillToolSpec, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None


@dataclass(slots=True)
class SkillActivation:
    """一组 Skill 激活后交给 Agent 的提示词和工具定义。"""

    skills: tuple[SkillSpec, ...]
    instructions: str
    tool_names: tuple[str, ...]
    tool_definitions: list[dict[str, Any]]

    def compose_system_prompt(self, base_prompt: str) -> str:
        """把 Skill 指令追加到应用的基础系统提示词。"""
        parts = [part.strip() for part in (base_prompt, self.instructions) if part.strip()]
        return "\n\n".join(parts)


__all__ = ["SkillActivation", "SkillSpec", "SkillToolKind", "SkillToolSpec"]
