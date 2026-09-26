# 变更日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，变更按
[Keep a Changelog](https://keepachangelog.com/zh-CN/) 的结构记录。

## [Unreleased]

### Added

- 基于 VitePress 的中文文档站、使用指南和公共 API 参考。
- 开源许可证、贡献指南、社区准则、安全策略、支持说明和发布检查清单。
- 最小 Agent、自定义模型和持久化工作流示例。
- Core 检查与文档构建的 CI 配置。

## [0.1.0] - 2026-09-26

### Added

- 手写 ReAct Agent Loop，支持普通执行、真正的流式输出、并发工具调用和运行恢复。
- Function Calling、MCP 客户端、Skill 清单加载与工具绑定。
- 模型协议、模型目录、运行时模型选择，以及 OpenAI、Anthropic、Gemini、Ollama 适配器。
- Middleware、HITL、Guardrails、可观测性和上下文压缩。
- Checkpoint、状态机、持久化工作流、运行租约和 ACP stdio 服务端。
- 内存、SQLite 和 PostgreSQL 的会话、上下文、运行、审批、工作流与模型选择存储。
- 用户、租户和管理员访问上下文。
