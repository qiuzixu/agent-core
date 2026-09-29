# Agent Core 已实现能力

本文只记录 `handwritten-agent-core` 已经实现并可被其他 Agent 项目复用的能力。
地图控制、业务规则、业务 API、前端页面等应用能力不属于 Agent Core。
整体模块关系和应用调用链见 [Agent Core 架构](ARCHITECTURE.md)。

## 1. 核心定位

Agent Core 是一套零外部平台绑定的手写 Agent 运行时内核。
它负责模型、Skill、工具、Agent Loop、运行生命周期、上下文、记忆、审批、工作流、持久化、
可观测性和 ACP 协议。上层项目负责业务工具、业务规则、提示词和应用接口。

## 2. 能力总览

| 模块 | 已实现能力 | 主要公开接口 |
| --- | --- | --- |
| `runtime` | ReAct Agent Loop、同步/流式运行、后台 Run、取消、checkpoint 恢复和 Worker 租约 | `ReActAgent`、`AgentRuntime` |
| `protocol` | 消息、运行上下文、运行事件、审批、工具结果和能力声明 | `Message`、`RunContext`、`RunEvent`、`ApprovalRecord`、`ToolResult`、`AgentCapabilities` |
| `model` | 模型协议、Provider 注册表、统一工厂、结构化输出、限流、并发、超时、熔断和 fallback | `ModelAdapter`、`StructuredOutputSpec`、`GovernedModelAdapter` |
| `skills` | Skill 清单加载、严格校验、注册、按需激活及 Function/MCP 绑定 | `SkillLoader`、`SkillRegistry`、`SkillSpec`、`SkillActivation` |
| `tools` | 工具注册、Schema、参数基础校验、超时、单个和批量并发执行 | `ToolRegistry`、`ToolExecutor`、`ToolSpec` |
| `mcp` | MCP 工具发现和调用、超时及统一错误 | `McpToolClient`、`McpToolCaller` |
| `middleware` | Agent、模型和工具执行前后的扩展链 | `Middleware`、`MiddlewareManager` |
| `callbacks` | 进程内生命周期事件订阅、同步/异步函数包装和订阅者故障隔离 | `CallbackHandler`、`CallbackManager` |
| `documents` | Blob、文档、片段、来源定位、纯文本加载和递归字符切分 | `Document`、`TextLoader`、`RecursiveCharacterTextSplitter` |
| `retrieval` | 检索协议、内存/Chroma/pgvector 存储及环境工厂 | `Embeddings`、`EmbeddingRetriever`、`create_vector_store` |
| `serialization` | 显式白名单、类型标识、Schema 版本和迁移函数 | `SerializerRegistry`、`SerializedEnvelope` |
| `compaction` | Token 估算、历史摘要、消息裁剪、工具结果瘦身、spill 和超长恢复 | `CompactionMiddleware`、`TokenLimitMiddleware`、`SpillStore` |
| `checkpoint` | 内存/文件 checkpoint、历史版本、回滚和时间旅行 | `MemoryCheckpointer`、`FileCheckpointer`、`TimeTravelCheckpointer` |
| `hitl` | 人工审批请求、等待、批准、拒绝、超时、跨进程决策同步和中间件接入 | `ApprovalQueue`、`HumanInTheLoopMiddleware` |
| `workflow` | 异步状态机、节点策略、重试、超时、幂等、pending write、恢复和 Mermaid 导出 | `StateMachine`、`NodeExecutionPolicy`、`DurableWorkflowRunner` |
| `multi-agent` 扩展 | Agent 注册、权限过滤、规则/模型路由、handoff、父子 Run、预算、租约和恢复 | `AgentRegistry`、`SupervisorAgent`、`RuntimeAgentInvoker` |
| `storage` | 会话、上下文、长期记忆、Run、审批、模型选择和工作流实例持久化 | 各类 `Memory*`、`Sqlite*`、`Postgres*Store` |
| `guardrails` | 关键词、长度、PII 脱敏和输出格式检查 | `GuardrailsMiddleware` |
| `prompts` | 安全文本/聊天模板、消息占位符、partial、Prompt 版本和历史查询 | `PromptTemplate`、`ChatPromptTemplate`、`PromptRegistry` |
| `observability` | 并发隔离的模型/工具耗时、调用计数、Thread/Agent 统计和可选 OTel | `ObservabilityMiddleware`、`setup_tracing` |
| `access` | 用户和租户访问上下文 | `AccessContext` |
| `ports` | 存储、审批、事件和长期记忆等依赖倒置接口 | `RunStore`、`SessionStore`、`ContextStore`、`MemoryStore`、`EventSink` |
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
- 通过 `CallbackManager` 向进程内订阅者发布同一份 `RunEvent`；
- 流式运行发布 `model_stream_chunk` 增量事件；
- `RunEvent` 记录 parent run、组件、tags、metadata、usage、错误和耗时字段；
- 将用户、租户、输入、状态和 checkpoint 一起保存。
- 可选 Run Lease，在多 Worker 环境中互斥认领 Run；
- 定期 heartbeat 续租，租约丢失时停止本地执行；
- 扫描过期租约，将异常退出的 Run 标记为中断并选择是否自动恢复。
- 可选 `require_access=True` 严格模式，创建 Run 时强制绑定用户和租户，查询、恢复和取消时
  强制提供 `AccessContext`。

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
- 脱敏模型目录，不向前端暴露 API Key、Base URL 等秘密配置；
- `StructuredOutputSpec` 的 JSON 提取、JSON Schema 子集校验、decoder 和失败重试；
- `GovernedModelAdapter` 的按 scope 并发控制、滑动窗口请求/输入 Token 限流和调用超时；
- 连续失败熔断、恢复探测和候选 `ModelAdapter` fallback。

结构化输出使用跨 Provider 的 Prompt 约束和本地校验，因此自定义 Provider 无需实现厂商专属
JSON Schema API。当前 Schema 支持常用的类型、字段、枚举、数组、长度和数值范围规则；复杂业务
校验可放在 decoder。Token 窗口治理统计估算的输入 Token，输出 Token 要等所有 Provider 统一
暴露 usage 后才能纳入精确配额。流式调用只有在尚未输出内容时才会 fallback，避免拼接重复文本。

模型 SDK 都是可选依赖。只使用 Core 的协议、工作流或存储时，不需要安装所有模型 SDK。

## 5. Skill、工具和 MCP

Skill 层已经实现：

- 从单个目录、`skill.json` 或 Skill 根目录递归加载清单；
- 从清单内联读取 instructions，或安全读取同一 Skill 目录内的 `SKILL.md`；
- 校验必填字段、未知字段、工具名称、JSON Schema、重复名称和指令文件路径越界；
- 注册、替换、移除、查询和批量加载 Skill；
- 按名称选择要激活的 Skill，并合并其 instructions；
- 将 Skill 声明的本地 Function 或 MCP Tool 绑定到 `ToolRegistry`；
- 只生成本次激活 Skill 的模型 Tool Schema；
- 在真正注册工具前完成依赖和冲突检查，避免失败后留下部分工具；
- 禁止清单动态导入 Python，所有 Function 和 MCP 客户端必须由应用显式注入。

一个 Skill 可以只提供 instructions、只提供工具，也可以同时提供两者。Core 负责通用加载和绑定，
具体 Skill 内容、业务 Function、MCP Server 地址和允许激活的 Skill 集合仍由应用 Agent 决定。

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

Callback 与 Middleware 的职责不同：Callback 只观察已经发生的生命周期事件，不能改变执行结果；
Middleware 才负责修改、重试、审批或停止。`CallbackManager` 隔离单个处理器异常，`EventSink` 则把
同一份 `RunEvent` 发送到 WebSocket、消息队列或审计系统。

## 7. 文档、检索和序列化

Core 已提供完整的零依赖检索边界：

- `Blob`、`Document`、`DocumentChunk` 和 `DocumentLocator` 保存内容、来源、checksum、页码和字符区间；
- `DocumentLoader`、`BlobParser` 和 `TextSplitter` 定义文档接入协议；
- `TextLoader`、`TextBlobParser` 和 `RecursiveCharacterTextSplitter` 提供纯文本基础实现；
- `Embeddings`、`Retriever`、`VectorStore` 和 `Reranker` 定义检索端口；
- `EmbeddingRetriever` 组合 Embedding 与 VectorStore；
- `InMemoryVectorStore` 支持余弦相似度、namespace、metadata filter 及用户/租户隔离；
- `ChromaVectorStore` 提供开发环境本地持久化，并把同步客户端调用移出异步事件循环；
- `PgVectorStore` 提供固定维度表、JSONB metadata 过滤、余弦 HNSW 索引及生产权限过滤；
- `create_vector_store` 默认按测试/开发/生产选择 Memory、Chroma、pgvector，并允许显式覆盖；
- `KeywordRetriever` 提供小型静态语料和测试用关键词检索。

`SerializerRegistry` 使用 `{type, schema_version, payload}` 信封，只恢复显式注册的类型并支持逐版本
迁移。默认支持 Document、Blob、Message、RunContext、RunEvent、ApprovalRecord、ToolResult 和
AccessContext。Core 不使用 pickle，也不会
根据输入中的 Python 类路径动态导入代码。

Chroma 和 pgvector 通过 optional extras 安装，基础安装不依赖它们。仓库中的独立
`handwritten-agent-core-mineru` 扩展包实现 `DocumentLoader` 和 `BlobParser`，调用自托管
`/file_parse` 并保留页码、block id、区块类型、来源 checksum 和解析器版本。OpenAI/Gemini
Embedding 由独立 `handwritten-agent-core-embeddings` 扩展包实现，并与 Ollama 共用批处理、并发、
重试、超时和维度校验。Qdrant、FAISS、Milvus 和通用 OCR 尚未提供具体适配器。MinerU 模型、
OCR 运行时和服务部署不属于 Core。

## 8. 上下文、会话和记忆

Core 将几类状态分开保存：

| 状态 | 作用 |
| --- | --- |
| Session | 保存完整的多轮消息历史 |
| Context | 保存当前 Thread 跨轮使用的结构化上下文和业务引用 |
| Memory | 保存跨 Session 检索的记忆记录、来源、TTL、版本和 metadata |
| RunContext | 保存一次运行的状态、事件、迭代次数、审批和 checkpoint |
| Checkpoint | 保存 Agent Loop 或工作流可恢复的执行位置 |
| WorkflowExecution | 保存工作流执行实例、状态、输入输出和版本 |

Session Store 在调用方传入 `AccessContext` 时，会在首次写入时绑定 Thread 所有者，之后按用户和
租户校验读取、计数、删除和列表操作。同租户管理员可以访问租户内会话；未传身份的旧调用路径继续
保持兼容，便于现有 Agent 分阶段迁移。Session Store 和 Context Store 都支持
`require_access=True`；启用后，遗漏身份或访问未绑定所有者的历史数据会直接失败。

`MemoryStore` 提供独立于 Thread 的长期记忆端口，并包含内存、SQLite 和 PostgreSQL 实现。
它支持 namespace、关键词检索、metadata 过滤、来源、TTL、版本冲突检测以及用户/租户隔离。
外部知识文档通过独立的 `Retriever` 和 `VectorStore` 处理；需要把长期记忆接入统一检索时，可以
实现 `MemoryRetriever` 适配器，而不是混合两类存储语义。

上下文压缩支持：

- 粗略 Token 估算和压缩阈值；
- 在合法消息边界切分，避免留下孤立的 tool 消息；
- 生成结构化历史摘要；
- 合并之前的摘要，避免每次从头摘要；
- 保留最近对话并重建消息历史；
- 裁剪过大的工具返回值；
- 将完整工具结果写入 `SpillStore`，再通过 spill 工具按需读取；
- 识别模型上下文溢出异常并进行一次压缩恢复。

## 9. Checkpoint 和工作流

Checkpoint 支持内存和文件后端。`TimeTravelCheckpointer` 在基础 Checkpointer 上增加版本历史、
指定版本读取、持久化回滚指针、整条 thread 删除和时间旅行能力。

通用工作流不依赖外部编排框架，支持：

- 同步或异步节点；
- 固定边和异步条件边；
- 字典状态增量合并；
- `START`、`END` 生命周期；
- 最大执行步数保护；
- 节点和路由异常统一包装；
- 节点级 timeout、异常范围、最大尝试次数和指数退避；
- 启用重试时强制声明 `idempotent=True`；
- `NodeExecutionContext` 向外部副作用提供稳定幂等键；
- `WorkflowPause` 主动中断；
- 节点执行前持久化 `workflow_step_pending`，完成后保存状态和下一节点；
- `DurableWorkflowRunner` 在节点边界保存状态和下一节点；
- 进程重启后从最后保存的下一节点恢复，避免重复执行已经完成的节点；
- 默认在失败后重新抛出异常，也可设置 `raise_on_failure=False` 返回已经持久化的失败执行实例；
- `describe()` 输出结构化图定义，`to_mermaid()` 导出 Mermaid 流程图。

工作流定义由上层项目编写，工作流执行实例可通过 `WorkflowExecutionStore` 持久化。

### Multi-Agent 扩展包

独立 `handwritten-agent-core-multi-agent` 扩展包实现：

- `AgentInvoker` 统一子 Agent 调用协议，`RuntimeAgentInvoker` 可直接包装现有 `AgentRuntime`；
- `AgentRegistry` 管理能力、别名、优先级、角色要求和每个 Agent 的并发上限；
- `CapabilityRouter` 做确定性路由，`ModelAgentRouter` 在权限过滤后的候选中做结构化模型选择；
- `SupervisorAgent` 执行 handoff 链，记录 task/run 父子关系、lineage、调用结果和统一 `RunEvent`；
- handoff、Agent 调用、同一 Agent 访问、超时、重试、并行数、Token 和费用预算；
- `WorkflowCoordinationStore` 复用 Memory/SQLite/PostgreSQL Workflow Store 保存每个 Agent 边界；
- 可选 `RunLeaseStore` 提供多 Worker 认领、心跳和失租中断；
- `run_sequence()`、`run_parallel()`、取消以及 interrupted/进程重启后的显式恢复。

扩展包不包含业务角色、业务 Agent 实现或自动生成 Agent。应用决定注册哪些 Agent、共享哪些上下文
以及 handoff 的业务条件。

## 10. HITL 人工审批

Core 提供两层审批能力：

- `ApprovalQueue`：创建审批请求并等待外部提交批准或拒绝结果；
- `HumanInTheLoopMiddleware`：在指定工具执行前触发审批。

审批状态、请求参数、用户、租户、创建时间和过期时间可写入 Runtime Store。
等待端会轮询持久化状态，因此另一个进程提交的审批结果也能被当前执行感知；审批服务重启后仍可
按审批 ID 加载并处理原请求。
`ApprovalQueue(require_access=True)` 会要求审批创建时绑定用户和租户，并要求查询、批准和拒绝操作
携带 `AccessContext`。`DurableWorkflowRunner` 也提供相同的严格模式。
Web 弹窗、消息通知和审批人选择属于上层应用职责。

## 11. 存储和并发

| 存储对象 | 内存 | SQLite | PostgreSQL |
| --- | :---: | :---: | :---: |
| Session 消息历史 | 支持 | 支持 | 支持 |
| Context 结构化记忆 | 支持 | 支持 | 支持 |
| 跨会话长期记忆 | 支持 | 支持 | 支持 |
| Run、事件和审批 | 支持 | 支持 | 支持 |
| 模型选择 | 支持 | 支持 | 支持 |
| 工作流执行实例 | 支持 | 支持 | 支持 |
| Multi-Agent 协调实例 | 支持 | 支持 | 支持 |
| Run Worker 租约 | 支持 | 支持 | 支持 |

推荐用途：内存用于单元测试和临时运行，SQLite 用于本地开发，PostgreSQL 用于生产环境。
Runtime Store 已包含版本号、乐观并发冲突、幂等键、Thread 所属用户/租户和陈旧 Run 标记能力。
`RunLeaseStore` 额外提供 Worker 认领、续租、释放和过期扫描；租约与 Run 状态分开保存，便于接入
独立任务队列或 Worker 服务。

## 12. ACP 协议

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

## 13. 可观测性和错误体系

可观测性模块记录模型调用、工具调用、耗时、成功失败、Thread 汇总和 Agent 汇总。
同一个中间件实例可被多个异步会话并发复用，调用计时和 span 使用 `ContextVar` 隔离，
`thread_id` 优先读取当前 `MiddlewareContext.metadata`，
并提供可选 OpenTelemetry 初始化入口。

统一错误体系覆盖配置、模型调用、限流、超时、模型不可用、输出校验、工具执行、MCP、
Checkpoint、状态机和 Middleware。上层 API 可以据此统一映射 HTTP 或协议错误。

## 14. 新 Agent 需要提供的内容

基于 Core 创建一个新 Agent 时，上层项目通常只需要提供：

1. 系统提示词和业务 Prompt；
2. 业务 Skill、Function、MCP 配置和允许激活的 Skill/工具范围；
3. 确定性业务工作流或业务编排器；
4. 业务数据模型、权限规则和外部服务客户端；
5. HTTP、SSE、WebSocket、CLI 或其他应用入口；
6. 前端页面和业务交互。

## 15. 当前不属于 Core 的能力

以下内容应留在具体 Agent 项目中：

- 特定领域的地图、IoT 或外部系统控制；
- MinerU/OCR 服务部署、云文档源、模型 Embedding SDK 和向量数据库部署运维；
- 业务规则、业务流程和业务数据模型；
- HTTP/SSE/WebSocket 路由和前端页面；
- 业务数据库表和业务 DTO；
- Agent 品牌、页面文案和部署端口；
- 业务特有的工作流节点。
- 具体 Agent 团队、角色说明、业务路由规则和允许共享的上下文。

这个边界保证 `agent-core` 可以被任何新 Agent 项目直接依赖。
