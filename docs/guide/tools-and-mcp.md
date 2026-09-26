# 工具、Function Calling 与 MCP

模型看到的是统一的 function-calling schema。工具实际执行端可以是本地 Python 函数，也可以是
MCP Server。`ToolExecutor` 对两者使用相同的执行结果和错误路径。

## 注册 Function

```python
registry = ToolRegistry()
registry.register(
    "lookup_flight",
    lookup_flight,
    "按航班号查询信息",
    parameters={
        "type": "object",
        "properties": {"flight_no": {"type": "string"}},
        "required": ["flight_no"],
    },
    risk_level="read",
    requires_approval=False,
    timeout_seconds=10,
    idempotent=True,
)
executor = ToolExecutor(registry)
```

执行器进行基础参数校验，支持同步和异步函数、单个工具执行和一批工具并发执行。
`ToolResult` 统一表示成功值、错误、耗时和元数据。

## 工具策略

| 字段 | 用途 |
| --- | --- |
| `risk_level` | 供应用或中间件区分读、写和高风险操作 |
| `requires_approval` | 在执行器层触发审批回调 |
| `timeout_seconds` | 限制单次工具执行时间 |
| `idempotent` | 声明重试是否安全，供上层策略判断 |

Core 不会仅根据 `risk_level` 自动授权。应用必须提供实际的审批回调和权限规则。

## 连接 MCP Server

安装 MCP extra：

```bash
pip install "handwritten-agent-core[mcp]"
```

`McpToolClient` 当前提供 stdio 客户端，通过命令和入口文件启动 MCP Server：

```python
from pathlib import Path

from agent_core import McpToolClient

async with McpToolClient(
    command="python",
    entrypoint=Path("servers/tools.py"),
    timeout_seconds=30,
    env={"SERVICE_URL": "http://127.0.0.1:3000"},
) as client:
    print(await client.list_tools())
    result = await client.call_tool("lookup", {"id": "42"})
```

客户端将连接失败、超时和工具失败分别映射为 `McpConnectionError`、`McpTimeoutError` 和
`McpToolError`。应用应在关闭服务时调用 `aclose()`；异步上下文管理器会自动完成清理。

## 把 MCP 工具暴露给 Agent

推荐通过 Skill 声明远端工具名，再在激活时注入命名 MCP 客户端。这样业务 Agent 可以选择本次
运行启用哪些工具，而 Core 不需要动态导入未知代码。详细格式见 [Skill](./skills.md)。

## 传输边界

Core 的 `McpToolClient` 是 Agent 到 MCP Server 的客户端适配。浏览器与 Cesium Gateway 的
WebSocket 连接属于应用基础设施，不由 Core 建立；应用可以实现 `McpToolCaller` Protocol，把
已有网关调用封装成同样的 `list_tools()` 和 `call_tool()` 接口。
