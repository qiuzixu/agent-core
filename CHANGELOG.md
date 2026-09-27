# 变更日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，变更按
[Keep a Changelog](https://keepachangelog.com/zh-CN/) 的结构记录。

## [Unreleased]

### Added

- 基于 Material for MkDocs 的中文文档站、使用指南和公共 API 参考。
- 开源许可证、贡献指南、社区准则、安全策略、支持说明和发布检查清单。
- 最小 Agent、自定义模型和持久化工作流示例。
- Core 检查与文档构建的 CI 配置。
- JSON Schema 结构化输出、decoder 和校验失败重试。
- 模型请求/输入 Token 限流、按 scope 并发、超时、熔断和 fallback。
- 工作流节点级重试、超时、退避、幂等键、pending write 和 Mermaid 导出。
- 跨会话长期记忆端口及内存、SQLite、PostgreSQL 实现。
- 安全 Prompt 文本/聊天模板、消息占位符、partial 和默认变量。
- 生命周期 CallbackManager、增强 RunEvent 和流式增量事件。
- 文档加载/切分、Embedding/Retriever/VectorStore 协议及轻量内存实现。
- 独立 `handwritten-agent-core-mineru` 扩展包，支持自托管 `/file_parse`、区块、页码和来源追踪。
- 独立 `handwritten-agent-core-embeddings` 扩展包，支持 OpenAI、Gemini、Ollama 和统一工厂。
- 独立 `handwritten-agent-core-multi-agent` 扩展包，支持 Agent 注册、路由、handoff、父子 Run、
  预算、恢复、租约和顺序/并行协调。
- Chroma 本地持久化、PostgreSQL pgvector 生产适配器及统一向量存储工厂。
- 带类型白名单、Schema 版本和迁移函数的安全 JSON 序列化。

## [0.1.0] - 2026-09-26

### Added

- 手写 ReAct Agent Loop，支持普通执行、真正的流式输出、并发工具调用和运行恢复。
- Function Calling、MCP 客户端、Skill 清单加载与工具绑定。
- 模型协议、模型目录、运行时模型选择，以及 OpenAI、Anthropic、Gemini、Ollama 适配器。
- Middleware、HITL、Guardrails、可观测性和上下文压缩。
- Checkpoint、状态机、持久化工作流、运行租约和 ACP stdio 服务端。
- 内存、SQLite 和 PostgreSQL 的会话、上下文、运行、审批、工作流与模型选择存储。
- 用户、租户和管理员访问上下文。
