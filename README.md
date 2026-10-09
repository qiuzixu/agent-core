# Handwritten Agent Core

一个零外部平台绑定的 Python Agent 运行时内核。项目从头实现 Agent Loop、
工具调用、MCP、Skill、上下文压缩、持久化恢复、工作流、HITL、多模型适配和 ACP 协议。

> 当前版本为 `0.1.0` Alpha，要求 Python 3.13。公开 API 在 `0.x` 阶段仍可能调整。

## 主要能力

- 普通和流式 ReAct Agent Loop，本轮多个工具并发执行；
- Function Calling、MCP stdio 客户端和声明式 Skill；
- OpenAI、Anthropic、Gemini、Ollama，以及自定义模型 Provider；
- JSON Schema 结构化输出，以及模型限流、并发、超时、熔断和 fallback；
- Middleware、Guardrails、HITL、调用统计和可选 OpenTelemetry；
- 生命周期 Callback、统一 RunEvent 和流式增量事件；
- Document/Blob、文本加载与切分、Embedding/Retriever/VectorStore 协议；
- 可选 `handwritten-agent-core-mineru` 扩展包解析 PDF、扫描件和复杂版面；
- 可选 `handwritten-agent-core-embeddings` 扩展包接入 OpenAI、Gemini 和 Ollama；
- 可选 `handwritten-agent-core-multi-agent` 扩展包编排多个独立 Agent；
- 内存、Chroma、pgvector 向量存储，关键词检索以及用户/租户/namespace 隔离；
- 带类型白名单、Schema 版本和迁移函数的安全 JSON 序列化；
- 安全文本/聊天 Prompt 模板、消息占位符、partial 和版本管理；
- 基于模型上下文用量的摘要压缩、工具结果瘦身和 spill；
- Run、事件、幂等键、Worker 租约、心跳、重启恢复和取消；
- Checkpoint 历史、回滚、节点重试/幂等/pending write 和 Mermaid 状态机导出；
- 会话、长期上下文、审批、模型选择的内存、SQLite、PostgreSQL 存储；
- 跨会话长期记忆、TTL、版本冲突、关键词检索和访问隔离；
- 用户、租户和管理员访问上下文；
- ACP JSON-RPC/stdio 服务端适配。

完整边界见[已实现能力](docs/CAPABILITIES.md)，模块和应用调用关系见
[系统架构](docs/ARCHITECTURE.md)。

## 快速开始

推荐使用 [uv](https://docs.astral.sh/uv/)（与仓库其它项目一致，自动创建 `.venv`）：

```bash
cd agent-core
uv sync --all-extras
uv run python examples/basic_agent.py
```

`uv sync` 默认安装项目本体与 `dev` 依赖组；按需追加 extra：

```bash
uv sync --extra openai            # 单个 extra
uv sync --extra models,mcp,production
uv sync --all-extras              # 全部可选依赖
```

不用 uv 时也可以用标准 venv + pip：

```bash
cd agent-core
python -m venv .venv
python -m pip install -e ".[dev]"
python examples/basic_agent.py
```

`examples/basic_agent.py` 使用离线演示模型，不需要 API Key 或外部服务。

真实模型按需安装（uv 用户把 `pip install` 换成 `uv pip install`，或用上面的
`uv sync --extra ...`）：

```bash
pip install "handwritten-agent-core[openai]"
pip install "handwritten-agent-core[anthropic]"
pip install "handwritten-agent-core[gemini]"
pip install "handwritten-agent-core[models,mcp,production]"
pip install "handwritten-agent-core[chroma]"    # 本地持久化向量存储
pip install "handwritten-agent-core[pgvector]"  # PostgreSQL 向量存储
pip install handwritten-agent-core-mineru       # 独立 MinerU 文档解析扩展包
pip install "handwritten-agent-core-embeddings[openai]"
pip install "handwritten-agent-core-embeddings[gemini]"
pip install "handwritten-agent-core-embeddings[ollama]"
pip install handwritten-agent-core-multi-agent  # 独立多 Agent 编排扩展包
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

应用可以通过路径依赖使用尚未发布的 Core。**uv 0.11 起 path 源默认非 editable**，
不加 `editable = true` 时 site-packages 里是同步时刻的快照，Core 源码更新后应用
会用到旧代码（症状是新符号 ImportError）：

```toml
[project]
dependencies = ["handwritten-agent-core"]

[tool.uv.sources]
handwritten-agent-core = { path = "../agent-core", editable = true }
```

随后在应用目录执行 `uv sync`（多包仓库在 workspace 根执行
`uv sync --all-packages --all-extras`）完成安装。

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

贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
安全问题按 [SECURITY.md](SECURITY.md) 私密报告。首次发布前的外部配置和决策记录在
[OPEN_SOURCE_CHECKLIST.md](OPEN_SOURCE_CHECKLIST.md)。

## 许可证

项目按 [Apache License 2.0](LICENSE) 发布。


<!-- 嵌入式 Agent 内核 这个名字备选 -->
