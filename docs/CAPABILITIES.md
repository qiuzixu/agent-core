# Agent Core 已实现能力

本文只记录 `handwritten-agent-core` 已经实现并可被其他 Agent 项目复用的能力。
Cesium、低空航线、业务 API、React 页面等应用能力不属于 Agent Core。

## 1. 核心定位

Agent Core 是一套不依赖 LangChain、LangGraph 或 FastAPI 的手写 Agent 框架核心。
它负责模型、工具、Agent Loop、运行生命周期、上下文、记忆、审批、工作流、持久化、
可观测性和 ACP 协议。上层项目负责业务工具、业务规则、提示词和应用接口。

## 2. 能力总览

| 模块 | 已实现能力 | 主要公开接口 |
| --- | --- | --- |
| `runtime` | ReAct Agent Loop、同步运行、流式运行、后台 Run、取消和 checkpoint 恢复 | `ReActAgent`、`AgentRuntime` |
| `protocol` | 消息、运行上下文、运行事件、审批、工具结果和能力声明 | `Message`、`RunContext`、`RunEvent`、`ApprovalRecord`、`ToolResult`、`AgentCapabilities` |
| `model` | 模型协议、Provider 注册表、统一工厂、上下文使用量和安全模型目录 | `ModelAdapter`、`ModelProviderRegistry`、`create_model_provider` |
| `tools` | 工具注册、Schema、参数基础校验、超时、单个和批量并发执行 | `ToolRegistry`、`ToolExecutor`、`ToolSpec` |
| `mcp` | MCP 工具发现和调用、超时及统一错误 | `McpToolClient`、`McpToolCaller` |
| `middleware` | Agent、模型和工具执行前后的扩展链 | `Middleware`、`MiddlewareManager` |
| `compaction` | Token 估算、历史摘要、消息裁剪、工具结果瘦身、spill 和超长恢复 | `CompactionMiddleware`、`TokenLimitMiddleware`、`SpillStore` |
| `checkpoint` | 内存/文件 checkpoint、历史版本、回滚和时间旅行 | `MemoryCheckpointer`、`FileCheckpointer`、`TimeTravelCheckpointer` |
| `hitl` | 人工审批请求、等待、批准、拒绝、超时和中间件接入 | `ApprovalQueue`、`HumanInTheLoopMiddleware` |
| `workflow` | 异步状态机、节点、固定边、条件边和最大步数保护 | `StateMachine`、`StateMachineBuilder` |
| `storage` | 会话、上下文、Run、审批、模型选择和工作流实例持久化 | 各类 `Memory*`、`Sqlite*`、`Postgres*Store` |
| `guardrails` | 关键词、长度、PII 脱敏和输出格式检查 | `GuardrailsMiddleware` |
| `prompts` | Prompt 注册、版本保存、当前版本切换和历史查询 | `PromptRegistry` |
| `observability` | 模型/工具耗时、调用计数、Thread/Agent 统计和可选 OTel | `ObservabilityMiddleware`、`setup_tracing` |
| `access` | 用户和租户访问上下文 | `AccessContext` |
| `ports` | 存储、审批、事件等依赖倒置接口 | `RunStore`、`SessionStore`、`ContextStore`、`EventSink` |
| `acp` | ACP JSON-RPC/stdio、会话、提示、更新、取消和审批请求 | `AcpStdioServer`、`AcpBackend` |

## 3. Agent Loop 和运行生命周期

`ReActAgent` 已实现完整的手写 Agent Loop：

- 调用模型并解析文本与工具调用；
- 执行一批工具调用，支持并发执行；
- 把工具结果写回消息历史后进入下一轮模型调用；
- 普通运行和逐块流式输出；
- 最大迭代次数和最大工具调用次数保护；
- 每轮运行事件、状态和 checkpoint 更新；
- 工具审批、中间件停止和异常状态收敛；
- 上下文超长后压缩并重试模型调用。

`AgentRuntime` 在 Agent Loop 外提供 Run 生命周期：

- 创建后台 Run 或同步等待 Run；
- 查询、等待和取消运行；
- 从持久化 checkpoint 恢复同一个 Run；
- 使用 `idempotency_key` 防止重复创建；
- 发布新增的 `RunEvent`；
- 将用户、租户、输入、状态和 checkpoint 一起保存。

## 4. 模型层

模型层通过 `ModelAdapter` 统一普通响应和流式响应。目前包含：

- OpenAI 适配器；
- Anthropic 适配器；
- Gemini 适配器；
- Ollama 适配器；
- OpenAI Compatible 接口，可用于兼容服务和部分 Qwen 部署；
- Provider 注册机制，可由上层项目增加自定义模型；
- 统一模型工厂和旧 `create_llm_provider` 兼容入口；
- 模型上下文窗口查询和 Token 使用量结构；
- 脱敏模型目录，不向前端暴露 API Key、Base URL 等秘密配置。

模型 SDK 都是可选依赖。只使用 Core 的协议、工作流或存储时，不需要安装所有模型 SDK。

## 5. 工具和 MCP

工具系统已经实现：

- 工具名称、描述、参数 Schema 和处理函数注册；
- 重名检查和未知工具检查；
- 必填参数及基础类型校验；
- 同步和异步工具统一调用；
- 工具超时、异常包装和结构化 `ToolResult`；
- 多个工具调用并发执行；
- 工具定义转换为模型 Tool Calling 格式。

MCP 层已经实现通用 Client 适配，包括工具列表发现、工具调用、连接错误、调用超时和工具执行错误。
具体 MCP Server 地址、认证方式和允许暴露的工具仍由上层 Agent 配置。

## 6. Middleware 和 Guardrails

Middleware 可在 Agent、模型和工具执行前后介入，并返回继续、修改、重试或停止动作。
当前内置能力包括：

- 运行日志；
- 模型失败重试；
- Token 长度限制；
- 上下文压缩；
- 人工审批；
- 输入输出 Guardrails；
- 调用统计和链路追踪。

Guardrails 当前提供关键词拦截、文本长度限制、PII 脱敏和输出格式检查。
业务安全规则仍应由上层业务工作流或业务服务执行。

## 7. 上下文、会话和记忆

Core 将几类状态分开保存：

| 状态 | 作用 |
| --- | --- |
| Session | 保存完整的多轮消息历史 |
| Context | 保存跨轮使用的结构化长期上下文和业务引用 |
| RunContext | 保存一次运行的状态、事件、迭代次数、审批和 checkpoint |
| Checkpoint | 保存 Agent Loop 或工作流可恢复的执行位置 |
| WorkflowExecution | 保存工作流执行实例、状态、输入输出和版本 |

上下文压缩支持：

- 粗略 Token 估算和压缩阈值；
- 在合法消息边界切分，避免留下孤立的 tool 消息；
- 生成结构化历史摘要；
- 合并之前的摘要，避免每次从头摘要；
- 保留最近对话并重建消息历史；
- 裁剪过大的工具返回值；
- 将完整工具结果写入 `SpillStore`，再通过 spill 工具按需读取；
- 识别模型上下文溢出异常并进行一次压缩恢复。

## 8. Checkpoint 和工作流

Checkpoint 支持内存和文件后端。`TimeTravelCheckpointer` 在基础 Checkpointer 上增加版本历史、
指定版本读取和回滚能力。

通用工作流不依赖 LangGraph，支持：

- 同步或异步节点；
- 固定边和异步条件边；
- 字典状态增量合并；
- `START`、`END` 生命周期；
- 最大执行步数保护；
- 节点和路由异常统一包装。

工作流定义由上层项目编写，工作流执行实例可通过 `WorkflowExecutionStore` 持久化。

## 9. HITL 人工审批

Core 提供两层审批能力：

- `ApprovalQueue`：创建审批请求并等待外部提交批准或拒绝结果；
- `HumanInTheLoopMiddleware`：在指定工具执行前触发审批。

审批状态、请求参数、用户、租户、创建时间和过期时间可写入 Runtime Store。
Web 弹窗、消息通知和审批人选择属于上层应用职责。

## 10. 存储和并发

| 存储对象 | 内存 | SQLite | PostgreSQL |
| --- | :---: | :---: | :---: |
| Session 消息历史 | 支持 | 支持 | 支持 |
| Context 结构化记忆 | 支持 | 支持 | 支持 |
| Run、事件和审批 | 支持 | 支持 | 支持 |
| 模型选择 | 支持 | 支持 | 支持 |
| 工作流执行实例 | 支持 | 支持 | 支持 |

推荐用途：内存用于单元测试和临时运行，SQLite 用于本地开发，PostgreSQL 用于生产环境。
Runtime Store 已包含版本号、乐观并发冲突、幂等键、Thread 所属用户/租户和陈旧 Run 标记能力。

## 11. ACP 协议

ACP 模块提供通用 JSON-RPC/stdio 服务端，已经支持：

- `initialize`；
- `session/new`；
- 可选 `session/load`；
- `session/prompt`；
- `session/cancel`；
- `session/update` 流式通知；
- `session/request_permission` 双向审批请求；
- 文本、思考过程、工具开始和工具结束更新。

Core 只定义 `AcpBackend` 端口和协议服务端。具体 Agent 负责把自己的 Runtime、会话和审批机制
适配到该端口。

## 12. 可观测性和错误体系

可观测性模块记录模型调用、工具调用、耗时、成功失败、Thread 汇总和 Agent 汇总，
并提供可选 OpenTelemetry 初始化入口。

统一错误体系覆盖配置、模型调用、限流、超时、模型不可用、输出校验、工具执行、MCP、
Checkpoint、状态机和 Middleware。上层 API 可以据此统一映射 HTTP 或协议错误。

## 13. 新 Agent 需要提供的内容

基于 Core 创建一个新 Agent 时，上层项目通常只需要提供：

1. 系统提示词和业务 Prompt；
2. 业务工具、MCP 配置和允许调用的工具范围；
3. 确定性业务工作流或业务编排器；
4. 业务数据模型、权限规则和外部服务客户端；
5. HTTP、SSE、WebSocket、CLI 或其他应用入口；
6. 前端页面和业务交互。

## 14. 当前不属于 Core 的能力

以下内容应留在具体 Agent 项目中：

- Cesium 地图控制和 Cesium MCP Gateway 配置；
- 低空航线、飞行计划、空域、审批等业务规则；
- FastAPI 路由和 Web React 接口；
- 具体业务数据库表和业务 DTO；
- Agent 品牌、页面文案和部署端口；
- 某个业务特有的工作流节点。

这个边界保证 `agent-core` 可以被低空 Agent 以外的新 Agent 项目直接依赖。
