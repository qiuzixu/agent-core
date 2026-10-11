# Zero Agent Core

[![PyPI version](https://img.shields.io/pypi/v/zero-agent-core)](https://pypi.org/project/zero-agent-core/)
[![Python versions](https://img.shields.io/pypi/pyversions/zero-agent-core)](https://pypi.org/project/zero-agent-core/)
[![Core CI](https://github.com/qiuzixu/agent-core/actions/workflows/ci.yml/badge.svg)](https://github.com/qiuzixu/agent-core/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org/)

一个零外部平台绑定的 Python Agent 运行时内核。项目实现的 Agent Loop、工具调用、MCP、Skill、上下文压缩、持久化恢复、工作流、HITL、多模型适配和 ACP 协议。

> 当前版本：`0.2.1` Alpha，要求 Python 3.13。公开 API 在 `0.x` 阶段仍可能调整。

## 主要能力

- 普通和流式 ReAct Agent Loop，本轮多个工具并发执行；
- Function Calling、MCP stdio 客户端和声明式 Skill；
- OpenAI、Anthropic、Gemini、Ollama，以及自定义模型 Provider；
- JSON Schema 结构化输出，以及模型限流、并发、超时、熔断和 fallback；
- Middleware、Guardrails、HITL、调用统计和可选 OpenTelemetry；
- 生命周期 Callback、统一 RunEvent 和流式增量事件；
- Document/Blob、文本加载与切分、Embedding/Retriever/VectorStore 协议；
- 可选 `zero-agent-core-mineru` 扩展包解析 PDF、扫描件和复杂版面；
- 可选 `zero-agent-core-embeddings` 扩展包接入 OpenAI、Gemini 和 Ollama；
- 可选 `zero-agent-core-multi-agent` 扩展包编排多个独立 Agent；
- 内存、Chroma、pgvector 向量存储，关键词检索以及用户/租户/namespace 隔离；
- 带类型白名单、Schema 版本和迁移函数的安全 JSON 序列化；
- 安全文本/聊天 Prompt 模板、消息占位符、partial 和版本管理；
- 基于模型上下文用量的摘要压缩、工具结果瘦身和 spill；
- Run、事件、幂等键、Worker 租约、心跳、重启恢复和取消；
- Checkpoint 历史、回滚、节点重试、幂等/pending write 和 Mermaid 状态机导出；
- 会话、长期上下文、审批、模型选择的内存、SQLite、PostgreSQL 存储；
- 跨会话长期记忆、TTL、版本冲突、关键词检索和访问隔离；
- 用户、租户和管理员访问上下文；
- ACP JSON-RPC/stdio 服务端适配。

完整边界见 [CAPABILITIES.md](docs/CAPABILITIES.md)，模块和应用调用关系见 [系统架构](docs/ARCHITECTURE.md)。

## 选型参考：先看生态，再看边界

先说结论：`zero-agent-core` 不是“更好的 Agent 框架”，也不是成熟框架的替代品。它只是一个较轻的运行时内核，能力边界更窄，生态、连接器、示例和社区资料也远少于成熟框架。多数团队如果要快速验证想法或交付业务，优先考虑成熟框架通常更稳妥。

| 场景 | 优先考虑 | 原因 |
| --- | --- | --- |
| 快速跑通 Demo / 验证想法 | LangChain / LangGraph / CrewAI / AG2 / OpenAI Agents SDK | 生态、文档和示例更丰富 |
| 需要大量现成集成 | LangChain / LangGraph | 连接器和社区实践更成熟 |
| 角色化多 Agent 团队 | CrewAI | 角色和协作抽象更直接 |
| 多 Agent 对话 / 协作讨论 | AG2 | 更偏对话式求解 |
| 深度使用 OpenAI 平台链路 | OpenAI Agents SDK | 与 OpenAI 相关工具和运行链路集成更顺 |
| 复杂流程编排 | LangGraph | 状态图、持久化和平台生态更成熟 |
| 需要嵌入式 Agent Loop、状态恢复、HITL 和自定义存储边界 | zero-agent-core | 把这层当作可嵌入内核；但生态、连接器和长期维护成本需要自己承担 |

如果下面这些条件不明显，可以先不选它：

- 业务系统已经有自己的鉴权、存储和运行边界；
- Agent Loop、恢复语义、审批流必须嵌进现有系统，而不是作为独立平台运行；
- 你愿意接受较少的现成组件，并自己维护扩展点。

## 快速开始
## 快速开始

从 PyPI 安装（导入名是 `agent_core`，不受发行名更名影响）：

```bash
pip install zero-agent-core
```

推荐使用 [uv](https://docs.astral.sh/uv/)。如果还没有安装：

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
cd agent-core
uv sync --all-extras
uv run python examples/starter/starter_agent.py
uv run python examples/basic_agent.py
```

这两个示例都使用离线演示模型，不需要 API Key 或外部服务。

## 最小示例

```python
from agent_core import ReActAgent, ToolExecutor, ToolRegistry

def add(a: int, b: int) -> int:
    """返回两个整数之和。"""
    return a + b

registry = ToolRegistry()
registry.register(
    "add",
    add,
    "计算两个整数之和",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)

agent = ReActAgent(
    llm=DemoModel(),  # 也可以用 create_model_provider(config)
    tool_executor=ToolExecutor(registry),
    system_prompt="你是一个计算助手。",
    tool_definitions=registry.build_tool_definitions(),
)

asyncio.run(agent.run("帮我计算 20 + 22", thread_id="demo"))
```

完整单文件示例见 [`examples/starter/starter_agent.py`](examples/starter/starter_agent.py)：离线可运行、不需要任何 API Key，依次演示 `run`、`stream` 和一个 `requires_approval=True` 写工具的审批回调，并演示了如何用 `FileCheckpointer` 把会话状态落盘、跨进程恢复。更多示例（自定义模型、持久化工作流）在 [`examples/`](examples/) 目录，系统文档在 [`docs/guide/`](docs/guide/)。

## 按需安装

```bash
pip install "zero-agent-core[openai]"
pip install "zero-agent-core[anthropic]"
pip install "zero-agent-core[gemini]"
pip install "zero-agent-core[models,mcp,production]"
pip install "zero-agent-core[chroma]"    # 本地持久化向量存储
pip install "zero-agent-core[pgvector]"  # PostgreSQL 向量存储
pip install zero-agent-core-mineru       # 独立 MinerU 文档解析扩展包
pip install "zero-agent-core-embeddings[openai]"
pip install "zero-agent-core-embeddings[gemini]"
pip install "zero-agent-core-embeddings[ollama]"
pip install zero-agent-core-multi-agent  # 独立多 Agent 编排扩展包
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

## 文档站

文档源文件位于 `docs/`，使用 Material for MkDocs：

```bash
python -m pip install mkdocs-material
python -m mkdocs serve       # 本地预览 http://127.0.0.1:8000
python -m mkdocs build       # 构建到 site/
```

子路径部署时设置 `DOCS_BASE`，例如 `/zero-agent-core/`。文档入口为
[docs/index.md](docs/index.md)，包含快速开始、核心概念、模型、工具、MCP、Skill、存储、工作流、ACP 和公共 API 参考。

## 项目边界

Core 只提供框架通用能力（Agent Loop、模型适配、工具执行、会话与存储等），不包含任何特定业务。应用侧仍负责：

- HTTP、WebSocket 或 SSE 等 API 接入；
- 身份认证、业务授权和密钥读取；
- 业务提示词、业务工具和业务数据模型；
- 前端通信与外部系统集成；
- 部署编排、监控后端和数据库运维。

新 Agent 直接依赖 `agent_core`。Core 不感知、也不依赖任何具体业务域。

## 开发

```bash
uv run ruff check src tests examples
uv run pytest
uv run mkdocs build
uv build
```

不使用 uv 时：

```bash
python -m pip install build
ruff check src tests examples
pytest
python -m mkdocs build
python -m build
```

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私密报告。首次发布前的外部配置和决策记录在 [OPEN_SOURCE_CHECKLIST.md](OPEN_SOURCE_CHECKLIST.md)。

## 许可证

项目按 [Apache License 2.0](LICENSE) 发布。
