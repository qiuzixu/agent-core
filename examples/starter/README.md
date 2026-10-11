# Starter：五分钟上手 zero-agent-core

`starter_agent.py` 是一个**单文件、离线可运行**的最小完整示例：不需要 API Key、
不访问网络，直接 `python starter_agent.py` 就能看到 Agent Loop、工具调用、
审批回调、中间件日志和 checkpoint 的完整过程。

## 1. 安装

```bash
pip install zero-agent-core
```

需要 Python 3.13。要接真实模型时再按需安装 extras（如 `pip install "zero-agent-core[openai]"`）。

## 2. 运行

```bash
python starter_agent.py
```

脚本会依次演示：

1. `agent.run()` —— 阻塞式调用，返回最终回答；
2. `agent.stream()` —— 流式调用，逐块打印增量；
3. `save_note` 写工具 —— `requires_approval=True`，执行前先走审批回调
   （真实应用在这里接人工确认 / HITL）。

同时可以观察到：

- `LoggingMiddleware` 输出的 `[Log] before_model / before_tool / ...` 轨迹；
- `.starter_checkpoints/` 目录下按 thread 落盘的 JSON checkpoint。

## 3. 换成真实模型

打开 `starter_agent.py`，看 `build_llm_real_model_placeholder()` 里的注释：
安装对应 extras，构造配置对象，然后用 `create_model_provider(config)` 替换
`build_agent()` 中的 `DemoModel()` 即可，其余代码零改动。

## 4. 加自己的工具

在 `build_registry()` 里照抄一个 `registry.register(...)`：

- 普通只读工具：声明参数 JSON Schema 即可；
- 写操作工具：加 `risk_level="write"`、`requires_approval=True`，
  并在 `ToolExecutor(registry, approval_callback=...)` 里实现你的审批逻辑。

## 5. 加工作流 / 生产能力

多步骤编排、审批持久化、PostgreSQL 存储、租约与恢复等能力见下表。

## 指南导航

| 主题 | 文档 |
| --- | --- |
| 快速开始（离线最小 Agent） | `../../docs/guide/getting-started.md` |
| 工具、Function Calling 与 MCP | `../../docs/guide/tools-and-mcp.md` |
| 模型与适配器（OpenAI/Anthropic/Gemini/Ollama） | `../../docs/guide/models.md` |
| 存储与生产部署（SQLite/PostgreSQL） | `../../docs/guide/storage-and-production.md` |
| Multi-Agent 编排扩展包 | `../../docs/guide/multi-agent.md` |
| 文档、检索与回调 | `../../docs/guide/documents-retrieval-and-callbacks.md` |
| 公共 API 参考 | `../../docs/reference/public-api.md` |
| 配置参考 | `../../docs/reference/configuration.md` |
| 架构总览 | `../../docs/ARCHITECTURE.md` |
| 已实现能力清单 | `../../docs/CAPABILITIES.md` |
| 更多示例 | `../basic_agent.py`（最小示例）等 `examples/` 目录 |
