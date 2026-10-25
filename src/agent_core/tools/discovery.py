"""MCP 工具自动发现与注册。

把 MCP server 的所有工具（tools/list 返回的完整定义）自动翻译成
OpenAI function calling 格式并注册到 ToolRegistry，实现"即插即用"。

默认安全策略：
- 所有自动注册的工具默认 risk_level="read"、requires_approval=False
- 可通过参数覆盖为更严格的策略（如 requires_approval=True）
- 也可通过 ``include`` / ``exclude`` 只注册部分工具
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from agent_core.mcp.client import JsonObject, McpToolCaller
from agent_core.tools.executor import ToolRegistry

if TYPE_CHECKING:
    from agent_core.tools.config import McpDiscoveryConfig

logger = logging.getLogger(__name__)


def mcp_tool_to_function_def(tool: JsonObject) -> dict[str, Any]:
    """把 MCP 工具定义翻译成 OpenAI function calling 格式。

    MCP inputSchema 与 OpenAI parameters 本质上都是 JSON Schema，
    翻译几乎是无损的。
    """
    return {
        "type": "function",
        "function": {
            "name": str(tool.get("name", "")),
            "description": str(tool.get("description", "")),
            "parameters": tool.get("inputSchema") or {
                "type": "object", "properties": {}, "required": []
            },
        },
    }


async def auto_register_mcp_tools(
    client: McpToolCaller,
    registry: ToolRegistry,
    *,
    server_name: str = "",
    risk_level: str = "read",
    requires_approval: bool = False,
    timeout_seconds: float | None = None,
    idempotent: bool = True,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    replace_tools: bool = False,
    parameters_override: Mapping[str, dict[str, Any]] | None = None,
    descriptions_override: Mapping[str, str] | None = None,
) -> list[str]:
    """自动发现 MCP server 的所有工具并注册到 ToolRegistry。

    Args:
        client: MCP 客户端（需实现 list_tool_definitions 和 call_tool）。
        registry: 目标工具注册表。
        server_name: MCP server 名称，用于日志和命名空间前缀（可为空）。
        risk_level: 注册的所有工具默认风险等级。
        requires_approval: 注册的所有工具默认是否需要审批。
        timeout_seconds: 可选的全局超时秒数。
        idempotent: 注册的所有工具默认是否幂等。
        include: 只注册这些工具（为 None 则全部注册）。
        exclude: 排除这些工具。
        replace_tools: 是否覆盖同名已注册工具（默认拒绝覆盖）。
        parameters_override: 按工具名覆盖参数 schema（可选）。
        descriptions_override: 按工具名覆盖 description（可选）。

            第三方 MCP server 的描述质量参差不齐，覆盖后可以
            不改 server 就优化模型对该工具的调用准确率。
            优先级：override > server 返回的 description。

    Returns:
        实际注册的工具名称列表。
    """
    if not callable(getattr(client, "list_tool_definitions", None)):
        raise TypeError(
            f"MCP 客户端 {type(client).__name__} 未实现 list_tool_definitions()，"
            "无法自动发现工具。请升级到支持该方法的 McpToolClient 或自行实现。"
        )

    definitions = await client.list_tool_definitions()
    include_set = set(include) if include else None
    exclude_set = set(exclude) if exclude else set()
    overrides = parameters_override or {}
    description_overrides = descriptions_override or {}

    registered: list[str] = []
    for tool in definitions:
        name = str(tool.get("name", ""))
        if not name or name in exclude_set:
            continue
        if include_set is not None and name not in include_set:
            continue

        if not replace_tools and registry.get_tool(name) is not None:
            logger.warning("MCP 工具 %r 已在注册表中，跳过（replace_tools=False）", name)
            continue

        func = _make_mcp_wrapper(client, name)

        # 优先用 override 的参数 schema；JSON Schema 本质都是字典，
        # tool.get 返回 object，这里显式收窄（MCP 规范保证 inputSchema 是对象）。
        parameters = cast(
            dict[str, Any],
            overrides.get(name)
            or tool.get("inputSchema")
            or {"type": "object", "properties": {}, "required": []},
        )

        # 描述优先级：显式覆盖 > server 返回值（见 descriptions_override）。
        registry.register(
            name=name,
            func=func,
            description=description_overrides.get(name)
            or str(tool.get("description", "")),
            parameters=parameters,
            risk_level=risk_level,
            requires_approval=requires_approval,
            timeout_seconds=timeout_seconds,
            idempotent=idempotent,
        )
        registered.append(name)
        logger.debug(
            "MCP 工具已注册：%s%s（server=%s, risk=%s, approval=%s）",
            name,
            f"（来自 {server_name}）" if server_name else "",
            server_name or "unknown",
            risk_level,
            requires_approval,
        )

    if server_name:
        logger.info(
            "MCP 自动注册完成：server=%s, 共 %d/%d 个工具",
            server_name, len(registered), len(definitions),
        )
    return registered


def _make_mcp_wrapper(client: McpToolCaller, tool_name: str) -> Any:
    """为 MCP 工具创建一个异步包装函数，让 ToolExecutor 可以直接调用。"""

    async def call_mcp(**arguments: Any) -> dict[str, object]:
        return await client.call_tool(tool_name, arguments)

    call_mcp.__name__ = f"mcp_{tool_name}"
    call_mcp.__doc__ = f"自动生成的 MCP 工具包装：{tool_name}"
    return call_mcp


async def connect_mcp_server(
    client: McpToolCaller,
    registry: ToolRegistry,
    config: McpDiscoveryConfig | None = None,
    *,
    server_name: str = "",
) -> list[str]:
    """根据全局配置策略连接 MCP server 并注册工具。

    这是推荐入口——根据 McpDiscoveryConfig.mode 自动选择策略：
    - mode="auto"：自动发现所有工具并注册（即插即用，像 Cursor）
    - mode="skill"：不做自动注册（工具由 SkillRegistry.activate() 显式声明）

    Args:
        client: MCP 客户端。
        registry: 目标工具注册表。
        config: 发现策略配置（为 None 则从环境变量/配置中心读取）。
        server_name: MCP server 名称，用于日志。

    Returns:
        实际注册的工具名称列表（mode="skill" 时返回空列表）。
    """
    from agent_core.tools.config import McpDiscoveryConfig as _Config

    cfg = config or _Config.from_env()

    if cfg.mode == "skill":
        # 显式声明模式：不自动注册，交给 SkillRegistry 处理
        logger.debug(
            "MCP 连接 %s：mode=skill，跳过自动注册（工具由 SkillRegistry 声明）",
            server_name or "unnamed",
        )
        return []

    return await auto_register_mcp_tools(
        client,
        registry,
        server_name=server_name,
        risk_level=cfg.risk_level,
        requires_approval=cfg.requires_approval,
        timeout_seconds=cfg.timeout_seconds,
        idempotent=cfg.idempotent,
        include=cfg.include,
        exclude=cfg.exclude,
        replace_tools=cfg.replace_tools,
        descriptions_override=cfg.descriptions_override,
    )
