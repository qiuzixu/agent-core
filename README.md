# Handwritten Agent Core

可复用的手写 Agent 框架核心，不依赖 LangChain、LangGraph 或 FastAPI；
模型和数据库 SDK 通过可选 extras 按需启用。

当前公共能力：

- ReAct Agent Loop，支持普通和流式执行
- 工具注册、参数基础校验、超时、审批和并发执行
- Middleware、重试和上下文长度限制
- 提示词版本注册、输入输出 Guardrails 和可观测性统计
- 统一访问上下文、HITL 审批队列和审批存储端口
- 上下文压缩、工具结果瘦身、spill 和模型超长恢复
- RunContext、RunEvent、ApprovalRecord 和 ToolResult
- Checkpoint、历史版本和回滚
- 通用异步状态机
- 模型调用 Protocol、模型提供商注册表和统一模型工厂
- 脱敏模型目录和运行时模型选择协议
- 用户/租户级模型选择存储：测试内存、开发 SQLite、生产 PostgreSQL
- 会话、长期上下文、运行状态、审批和工作流执行的存储实现
  - 内存：测试和临时运行
  - SQLite：开发环境，零额外依赖
  - PostgreSQL：生产环境，按需安装 `asyncpg`

Core 提供模型工厂、Provider 注册机制以及 OpenAI、Anthropic、Gemini、Ollama
的具体适配器；模型 SDK 使用可选 extras 安装。Web API 和业务能力仍由上层应用提供。
Core 同时提供通用存储实现，具体业务表和业务字段由上层应用负责。

模型依赖可以按需安装：`handwritten-agent-core[openai]`、
`handwritten-agent-core[anthropic]`、`handwritten-agent-core[gemini]`、
`handwritten-agent-core[qwen]`，或一次安装全部模型依赖的
`handwritten-agent-core[models]`。Ollama 复用 OpenAI 兼容接口。

在本地应用中通过路径依赖接入：

```toml
[project]
dependencies = ["handwritten-agent-core"]

[tool.uv.sources]
handwritten-agent-core = { path = "../agent-core" }
```

Vanilla 项目原有的 `low_altitude_agent_vanilla.core` 和
`low_altitude_agent_vanilla.agent` 入口仍然保留，它们只是 Core 的兼容导出。
新 Agent 可以直接依赖 `agent_core`，业务项目只需要提供工具和业务 API；
如果有特殊模型或后端，也可以注册自定义适配器或替换 Core 的存储实现。

存储可以直接按环境创建：

```python
from agent_core.storage import create_context_store, create_runtime_store, create_session_store

sessions = create_session_store("development", sqlite_path="./agent.db")
contexts = create_context_store("development", sqlite_path="./agent.db")
runs = create_runtime_store("development", sqlite_path="./agent.db")
```

生产环境传入 `postgres_url` 并安装 `handwritten-agent-core[production]`；
PostgreSQL 存储的 `initialize()` 负责连接池和幂等建表，服务关闭时调用 `close()`。

```python
from agent_core import (
    ReActAgent,
    ToolExecutor,
    ToolRegistry,
    create_model_provider,
    register_model_provider,
)

# 应用可以直接使用 Core 内置 Provider，也可以注册自己的适配器。
model = create_model_provider(config)

registry = ToolRegistry()
registry.register("lookup", lookup, "查询数据")

agent = ReActAgent(
    llm=model,
    tool_executor=ToolExecutor(registry),
    system_prompt="你是一个助手",
    tool_definitions=registry.build_tool_definitions(),
)

answer = await agent.run("查询数据")
```
