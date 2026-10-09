# 变更日志

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)，变更按
[Keep a Changelog](https://keepachangelog.com/zh-CN/) 的结构记录。

## [Unreleased]

### Added

- `AgentRuntime` 支持可选 `run_executor`（`RunExecutor` 协议）：应用可接管 Agent 调用，
  Runtime 统一收口运行状态、事件、持久化与租约。
- 访问校验抽取为 `agent_core.access.enforce_access`：严格模式要求、资源归属完整性与
  身份校验三类判定共享同一实现。

### Fixed

- 直接 API 以 checkpoint 恢复 Agent 时回填 RunContext.checkpoint 基底，修复恢复批次进度快照丢失迭代/预算/最终回答的问题。
- 时间旅行接口 `get_checkpoint_history`/`rollback_to` 增加显式 `thread_id` 参数，避免读到构造时默认会话的版本。
- Middleware `MODIFY` 聚合与 ctx 写入对账，按注释契约只写回 ctx 的自定义中间件不再被 data 旧值覆盖。
- 并发工具批次末尾不再重复保存与主循环相同的 phase=model 快照。
- 幂等唯一索引升级前预检归一化后重复行，SQLite/PostgreSQL 初始化给出可操作的错误而不是建索引失败。
- HITL 幻影请求（存储记录缺失）等待超时按 TIMEOUT 收口；进程内缓存与 `list_pending` 补齐过期检查。
- 取消运行时记录任务的业务异常日志，不再无声吞掉。
- 模型治理在全部候选熔断时直接失败且不消耗限流配额，熔断状态容量可配置并自动淘汰。
- `TimeTravelCheckpointer` 持久化失败回滚按身份移除版本，避免并发保存时弹掉其他调用者的版本。

### Changed

- `ReActAgent` 的 `run`/`stream` 双循环抽取为共享内核 `_iterate`：消息构造、checkpoint 恢复、
  中间件 before/after、工具预算守卫、工具执行与快照保存只实现一次；非流式/流式一轮模型调用的
  差异（`llm.chat` vs `stream_chat` 增量归并、文案与日志标识）集中到 `_LoopMode` 策略对象。
  对外签名、事件序列、checkpoint 状态字段与错误文案逐字保持不变，并有 run/stream checkpoint
  一致性契约测试守护。
- `AgentRuntime` 与 `DurableWorkflowRunner` 的 `_check_access` 改为委托共享的
  `agent_core.access.enforce_access`，判定顺序与错误文案保持不变。
- 补齐 compaction 子系统核心单测（region 边界切割、pruner 瘦身与 spill 标记、summarizer 结构
  校验失败、中间件阈值触发与 prune-only 降级）、OpenAI/Anthropic/Gemini 流式 `finish_reason`
  契约测试，以及"恢复时只重放未完成工具"回归断言。
- 修复 Runtime/Workflow 并发覆盖和匿名作用域幂等索引，PostgreSQL Workflow JSONB 恢复兼容字符串返回。
- HITL 持久化审批 deadline，并以条件状态转换避免超时覆盖批准或拒绝。
- 文件与时间旅行 Checkpoint 改为原子替换，ReAct 并发工具按完成进度保存可恢复快照。
- 模型治理按逻辑请求执行限流和并发控制，增加默认调用超时与按 scope 隔离的熔断状态。
- 统一模型工具参数错误、结构化输出校验、MCP 文本结果回退和工具失败观测语义。
- 收紧 Context/Prompt/ACP 的归属、损坏文件和审批选项处理，并限制进程内观测记录容量。
- `ObservabilityMiddleware` 按异步上下文隔离模型/工具计时，并从当前调用元数据读取 `thread_id`。
- `DurableWorkflowRunner` 增加 `raise_on_failure`，应用可以读取已持久化的失败执行实例。
- `TimeTravelCheckpointer` 持久化 rollback 当前版本指针，并实现整条 thread 历史删除协议。

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
