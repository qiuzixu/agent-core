"""MCP 工具发现策略配置。

通过环境变量或配置中心（Apollo/Nacos）控制 MCP 工具的接入模式：

- ``auto``（即插即用）：像 Cursor 一样，接入 MCP server 后自动发现并注册所有工具
- ``skill``（显式声明）：通过 Skill manifest 手动声明每个工具的绑定关系

配置来源（优先级从高到低）：
1. 代码里直接传 McpDiscoveryConfig(...)
2. 环境变量 AGENT_MCP_DISCOVERY_MODE / AGENT_MCP_DISCOVERY_APPROVAL / ...
3. 配置中心 get("mcp.discovery.mode") / get("mcp.discovery.approval") / ...
4. 默认值（mode="skill"，安全优先）

环境变量示例：
    AGENT_MCP_DISCOVERY_MODE=auto
    AGENT_MCP_DISCOVERY_APPROVAL=false
    AGENT_MCP_DISCOVERY_RISK=read
    AGENT_MCP_DISCOVERY_INCLUDE=tool_a,tool_b     # 只注册这些
    AGENT_MCP_DISCOVERY_EXCLUDE=tool_x,tool_y     # 排除这些
    AGENT_MCP_DISCOVERY_REPLACE=true              # 允许覆盖同名工具
    AGENT_MCP_DISCOVERY_DESCRIPTIONS={"tool_a":"新描述"}   # JSON，按工具改写描述
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol


class _ConfigReader(Protocol):
    """配置中心最小协议（兼容 ConfigCenter / FileConfigCenter）。"""

    def get(self, key: str, default: Any = None) -> Any: ...


def _env_or_config(
    config_center: _ConfigReader | None,
    env_key: str,
    config_key: str,
    default: str,
) -> str:
    """从环境变量或配置中心读取字符串值，环境变量优先。"""
    env_val = os.environ.get(env_key)
    if env_val is not None:
        return env_val.strip()
    if config_center is not None:
        val = config_center.get(config_key)
        if val is not None:
            return str(val).strip()
    return default


def _env_or_config_bool(
    config_center: _ConfigReader | None,
    env_key: str,
    config_key: str,
    default: bool,
) -> bool:
    """读取布尔值（true/false/1/0/yes/no）。"""
    raw = _env_or_config(config_center, env_key, config_key, "").lower()
    if not raw:
        return default
    return raw in ("true", "1", "yes", "on")


def _split_list(raw: str) -> list[str]:
    """把逗号分隔的字符串拆成列表，去空格去空项。"""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_json_object(raw: str) -> dict[str, str] | None:
    """把 JSON 字符串解析为字符串字典；为空或非法时返回 None 并保持默认。

    用于描述覆盖等"按工具名 → 值"的映射配置，例如
    ``{"geocode": "地名转坐标，输入城市中文名"}``。
    """
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return {str(key): str(item) for key, item in value.items()}


@dataclass
class McpDiscoveryConfig:
    """MCP 工具发现策略。"""

    # 接入模式："auto" = 即插即用（自动发现所有工具）, "skill" = 显式声明（走 SkillRegistry）
    mode: str = "skill"
    # 自动注册的工具是否需要人工审批（mode="auto" 时生效）
    requires_approval: bool = False
    # 自动注册的工具默认风险等级
    risk_level: str = "read"
    # 自动注册的工具默认超时（None = 不限制，由 ToolExecutor 兜底）
    timeout_seconds: float | None = None
    # 是否幂等
    idempotent: bool = True
    # 白名单：只注册这些工具（None = 全部）
    include: list[str] | None = None
    # 黑名单：排除这些工具
    exclude: list[str] | None = None
    # 是否覆盖同名已注册工具
    replace_tools: bool = False
    # 描述覆盖：按工具名改写自动注册工具的 description（优化模型调用准确率）
    descriptions_override: dict[str, str] | None = None

    @classmethod
    def from_env(
        cls,
        *,
        config_center: _ConfigReader | None = None,
    ) -> McpDiscoveryConfig:
        """从环境变量 / 配置中心构建配置。

        优先级：环境变量 > 配置中心 > 默认值。

        Args:
            config_center: 可选的配置中心实例（如 Apollo / Nacos / FileConfigCenter）。
        """
        mode = _env_or_config(
            config_center, "AGENT_MCP_DISCOVERY_MODE", "mcp.discovery.mode", "skill"
        ).lower()
        if mode not in ("auto", "skill"):
            mode = "skill"

        include_raw = _env_or_config(
            config_center, "AGENT_MCP_DISCOVERY_INCLUDE", "mcp.discovery.include", ""
        )
        exclude_raw = _env_or_config(
            config_center, "AGENT_MCP_DISCOVERY_EXCLUDE", "mcp.discovery.exclude", ""
        )
        timeout_raw = _env_or_config(
            config_center, "AGENT_MCP_DISCOVERY_TIMEOUT", "mcp.discovery.timeout", ""
        )
        descriptions_raw = _env_or_config(
            config_center, "AGENT_MCP_DISCOVERY_DESCRIPTIONS", "mcp.discovery.descriptions", ""
        )

        return cls(
            mode=mode,
            requires_approval=_env_or_config_bool(
                config_center, "AGENT_MCP_DISCOVERY_APPROVAL", "mcp.discovery.approval", False
            ),
            risk_level=_env_or_config(
                config_center, "AGENT_MCP_DISCOVERY_RISK", "mcp.discovery.risk", "read"
            ),
            timeout_seconds=float(timeout_raw) if timeout_raw else None,
            idempotent=_env_or_config_bool(
                config_center, "AGENT_MCP_DISCOVERY_IDEMPOTENT", "mcp.discovery.idempotent", True
            ),
            include=_split_list(include_raw) or None,
            exclude=_split_list(exclude_raw) or None,
            replace_tools=_env_or_config_bool(
                config_center, "AGENT_MCP_DISCOVERY_REPLACE", "mcp.discovery.replace", False
            ),
            descriptions_override=_parse_json_object(descriptions_raw),
        )
