# Handwritten Agent Core

可复用的手写 Agent 框架核心，不依赖 LangChain、LangGraph、FastAPI、数据库或具体模型 SDK。

当前公共能力：

- ReAct Agent Loop，支持普通和流式执行
- 工具注册、参数基础校验、超时、审批和并发执行
- Middleware、重试和上下文长度限制
- RunContext、RunEvent、ApprovalRecord 和 ToolResult
- Checkpoint、历史版本和回滚
- 通用异步状态机
- 模型调用 Protocol

具体模型、数据库、Web API 和业务能力应由上层应用或 adapters 包提供。

在本地应用中通过路径依赖接入：

```toml
[project]
dependencies = ["handwritten-agent-core"]

[tool.uv.sources]
handwritten-agent-core = { path = "../agent-core" }
```

Vanilla 项目原有的 `low_altitude_agent_vanilla.core` 和
`low_altitude_agent_vanilla.agent` 入口仍然保留，它们只是 Core 的兼容导出。
新 Agent 可以直接依赖 `agent_core`，业务项目只需要提供模型适配器、工具和持久化实现。

```python
from agent_core import ReActAgent, ToolExecutor, ToolRegistry

registry = ToolRegistry()
registry.register("lookup", lookup, "查询数据")

agent = ReActAgent(
    llm=my_model_adapter,
    tool_executor=ToolExecutor(registry),
    system_prompt="你是一个助手",
    tool_definitions=registry.build_tool_definitions(),
)

answer = await agent.run("查询数据")
```
