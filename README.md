# Handwritten Agent Core

一个不依赖 LangChain、LangGraph 或 Web 框架的 Python Agent 核心。项目从头实现 Agent Loop、
工具调用、MCP、Skill、上下文压缩、持久化恢复、工作流、HITL、多模型适配和 ACP 协议。

> 当前版本为 `0.1.0` Alpha，要求 Python 3.13。公开 API 在 `0.x` 阶段仍可能调整。

## 主要能力

- 普通和流式 ReAct Agent Loop，本轮多个工具并发执行；
- Function Calling、MCP stdio 客户端和声明式 Skill；
- OpenAI、Anthropic、Gemini、Ollama，以及自定义模型 Provider；
- Middleware、Guardrails、HITL、调用统计和可选 OpenTelemetry；
- 基于模型上下文用量的摘要压缩、工具结果瘦身和 spill；
- Run、事件、幂等键、Worker 租约、心跳、重启恢复和取消；
- Checkpoint 历史、回滚、异步状态机和节点级持久化工作流；
- 会话、长期上下文、审批、模型选择的内存、SQLite、PostgreSQL 存储；
- 用户、租户和管理员访问上下文；
- ACP JSON-RPC/stdio 服务端适配。

完整边界见[已实现能力](docs/CAPABILITIES.md)，模块和应用调用关系见
[系统架构](docs/ARCHITECTURE.md)。

## 快速开始

```bash
cd agent-core
python -m venv .venv
python -m pip install -e ".[dev]"
python examples/basic_agent.py
```

`examples/basic_agent.py` 使用离线演示模型，不需要 API Key 或外部服务。

真实模型按需安装：

```bash
pip install "handwritten-agent-core[openai]"
pip install "handwritten-agent-core[anthropic]"
pip install "handwritten-agent-core[gemini]"
pip install "handwritten-agent-core[models,mcp,production]"
```

最小组装方式：

```python
from agent_core import ReActAgent, ToolExecutor, ToolRegistry, create_model_provider

model = create_model_provider(settings)

tools = ToolRegistry()
tools.register("lookup", lookup, "查询数据")

agent = ReActAgent(
    llm=model,
    tool_executor=ToolExecutor(tools),
    system_prompt="你是一个助手。",
    tool_definitions=tools.build_tool_definitions(),
)

answer = await agent.run("查询数据")
```

应用可以通过路径依赖使用尚未发布的 Core：

```toml
[project]
dependencies = ["handwritten-agent-core"]

[tool.uv.sources]
handwritten-agent-core = { path = "../agent-core" }
```

## 文档站

文档源文件位于 `docs/`，使用 Material for MkDocs：

```bash
python -m pip install mkdocs-material
python -m mkdocs serve       # 本地预览 http://127.0.0.1:8000
python -m mkdocs build       # 构建到 site/
```

子路径部署时设置 `DOCS_BASE`，例如 `/handwritten-agent-core/`。文档入口为
[docs/index.md](docs/index.md)，包含快速开始、核心概念、模型、工具、MCP、Skill、存储、
工作流、ACP 和公共 API 参考。

## 项目边界

Core 只提供框架通用能力（Agent Loop、模型适配、工具执行、会话与存储等），
不包含任何特定业务。应用侧仍负责：

- HTTP、WebSocket 或 SSE 等 API 接入；
- 身份认证、业务授权和密钥读取；
- 业务提示词、业务工具和业务数据模型；
- 前端通信与外部系统集成；
- 部署编排、监控后端和数据库运维。

新 Agent 直接依赖 `agent_core`。Core 不感知、也不依赖任何具体业务域。

## 开发

```bash
python -m pip install build
ruff check src tests examples
pytest
python -m mkdocs build
python -m build
```

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
安全问题按 [SECURITY.md](SECURITY.md) 私密报告。首次发布前的外部配置和决策记录在
[OPEN_SOURCE_CHECKLIST.md](OPEN_SOURCE_CHECKLIST.md)。

## 许可证

项目按 [Apache License 2.0](LICENSE) 发布。
