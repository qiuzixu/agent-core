# 公共 API

稳定使用入口是 `agent_core` 顶层包。下面按模块列出当前 `0.1.0` 的主要公共符号。
`0.x` 阶段 API 仍可能调整，破坏性变更会记录在 `CHANGELOG.md`。

## Agent 与运行时

| API | 作用 |
| --- | --- |
| `ReActAgent` | 普通和流式 ReAct Agent Loop |
| `AgentRuntime` | Run 生命周期、事件、恢复、取消、租约和访问检查 |
| `RunContext` / `RunEvent` / `RunStatus` | 运行状态和值对象 |
| `RuntimeRun` | 后台运行的上下文和进程内任务句柄 |
| `MemoryRunStore` / `MemoryEventSink` | 测试用运行与事件实现 |

## 消息与模型

| API | 作用 |
| --- | --- |
| `Message` | Provider 无关消息类型 |
| `system_message` / `user_message` / `assistant_message` / `tool_message` | 消息构造函数 |
| `ModelAdapter` | 模型实现协议 |
| `StreamChunk` / `ContextUsage` | 流式数据和上下文用量 |
| `create_model_provider` | 按名称创建模型适配器 |
| `register_model_provider` / `ModelProviderRegistry` | 自定义 Provider 注册 |
| `OpenAIProvider` / `AnthropicProvider` / `GeminiProvider` / `OllamaProvider` | 内置 Provider |
| `ModelOption` / `default_model_catalog` / `catalog_payload` | 可展示模型目录 |
| `StructuredOutputSpec` / `StructuredOutputResult` | 声明 JSON Schema、decoder 和结构化结果 |
| `chat_structured` / `parse_structured_output` / `validate_json_schema` | 结构化调用、解析和本地校验 |
| `GovernedModelAdapter` / `ModelExecutionPolicy` | 限流、并发、超时、熔断和 fallback |
| `model_execution_scope` | 设置本次异步调用链的租户或用户治理范围 |

`create_llm_provider` 是旧调用方的兼容入口，新代码使用 `create_model_provider`。

## 工具、MCP 与 Skill

| API | 作用 |
| --- | --- |
| `ToolSpec` / `ToolRegistry` / `ToolExecutor` | 工具声明、注册和执行 |
| `ToolResult` | 标准化执行结果 |
| `McpToolCaller` / `McpToolClient` | MCP 客户端协议与 stdio 实现 |
| `SkillLoader` / `SkillRegistry` | Skill 校验、加载和激活 |
| `SkillSpec` / `SkillToolSpec` / `SkillActivation` | Skill 值对象 |

## 中间件与策略

| API | 作用 |
| --- | --- |
| `Middleware` / `MiddlewareContext` / `MiddlewareResult` | 自定义中间件基础接口 |
| `MiddlewareAction` / `MiddlewareManager` | 中间件动作和调度 |
| `LoggingMiddleware` / `RetryMiddleware` | 日志和重试 |
| `TokenLimitMiddleware` / `CompactionMiddleware` | 硬截断和摘要压缩 |
| `GuardrailsMiddleware` 与各 `Guard` | 输入输出校验和脱敏 |
| `HumanInTheLoopMiddleware` | 工具级人工审批 |
| `ObservabilityMiddleware` | 延迟、调用量和可选 OTel span |

## Callback 与事件

| API | 作用 |
| --- | --- |
| `CallbackHandler` / `BaseCallbackHandler` | 进程内生命周期订阅协议和扩展基类 |
| `CallableCallbackHandler` | 把同步或异步函数包装成 Handler |
| `CallbackManager` / `CallbackFailure` | 有序分发、订阅管理和故障隔离 |
| `RunEvent` | Callback、EventSink、审计和前端共同使用的事件对象 |

Callback 只观察事件；需要修改或停止执行时使用 Middleware。`AgentRuntime` 支持直接传入
`callbacks`，也可以注入预先组装的 `callback_manager`。

## 文档与检索

| API | 作用 |
| --- | --- |
| `Blob` / `Document` / `DocumentChunk` / `DocumentLocator` | 原始内容、解析文档、切分片段和引用位置 |
| `DocumentLoader` / `BlobParser` / `TextSplitter` | 文档接入和切分协议 |
| `TextLoader` / `TextBlobParser` | 零依赖纯文本实现 |
| `RecursiveCharacterTextSplitter` | 按段落、换行和标点优先切分并保留 overlap |
| `Embeddings` / `Retriever` / `VectorStore` / `Reranker` | 检索依赖倒置协议 |
| `RetrievalQuery` / `RetrievalResult` / `SearchType` | 通用查询与结果值对象 |
| `EmbeddingRetriever` | 组合 Embeddings 和 VectorStore 的语义检索器 |
| `InMemoryVectorStore` / `KeywordRetriever` | 测试和小型语料实现 |
| `ChromaVectorStore` | 本地开发持久化向量存储 |
| `PgVectorStore` | PostgreSQL pgvector 生产存储 |
| `create_vector_store` / `VectorStoreBackend` | 环境默认与显式后端选择 |

具体 Embedding 模型、通用 OCR、FAISS、Milvus 和 Qdrant 通过上述协议扩展。

### Embedding Provider 扩展包

`handwritten-agent-core-embeddings` 从 `agent_core_embeddings` 导入：

| API | 作用 |
| --- | --- |
| `OpenAIEmbeddings` | OpenAI Embeddings API 适配器 |
| `GeminiEmbeddings` | Google Gen AI Embedding 适配器，区分文档和查询任务 |
| `OllamaEmbeddings` | Ollama 原生 `/api/embed` 适配器 |
| `EmbeddingExecutionPolicy` | 批大小、并发、超时、重试和预期维度 |
| `EmbeddingProviderRegistry` / `create_embeddings` | 自定义 Provider 注册和统一创建入口 |

扩展返回的对象全部满足 Core `Embeddings` Protocol，可以直接传给 `EmbeddingRetriever`。

### MinerU 扩展包

`handwritten-agent-core-mineru` 是独立发行包，从 `agent_core_mineru` 导入：

| API | 作用 |
| --- | --- |
| `MinerUDocumentLoader` / `MinerUBlobParser` | 自托管 MinerU 文档加载和 Blob 解析 |
| `MinerUConfig` / `MinerUTransport` | MinerU 请求配置和可替换传输端口 |
| `HttpxMinerUTransport` | `/file_parse` multipart HTTP 传输实现 |

云端批处理 API 可以通过实现 `MinerUTransport` 接入，不改变 Core。

## 安全序列化

| API | 作用 |
| --- | --- |
| `SerializedEnvelope` | `{type, schema_version, payload}` 版本化信封 |
| `SerializerRegistry` | 类型白名单、编码器、解码器和迁移函数注册 |
| `dumpd` / `dumps` / `load` / `loads` | 默认注册表的字典和 JSON 操作 |

默认注册表支持 Document、Blob、Message、RunContext、RunEvent、ApprovalRecord、ToolResult 和
AccessContext。它不会动态导入输入中的类，也不使用 pickle。

## 状态、工作流与审批

| API | 作用 |
| --- | --- |
| `Checkpointer` / `MemoryCheckpointer` / `FileCheckpointer` | 最新快照协议和实现 |
| `TimeTravelCheckpointer` / `CheckpointVersion` | 历史版本与回滚 |
| `StateMachineBuilder` / `StateMachine` | 通用异步状态机 |
| `NodeExecutionPolicy` / `NodeExecutionContext` | 节点重试、超时、退避和稳定幂等键 |
| `current_node_execution` | 在节点内读取当前尝试次数和幂等键 |
| `DurableWorkflowRunner` / `WorkflowPause` | 节点级持久化和中断恢复 |
| `ApprovalQueue` / `ApprovalRequest` / `ApprovalStatus` | 审批创建、等待和决策 |
| `ApprovalRecord` | 持久化审批值对象 |

## Multi-Agent 扩展包

`handwritten-agent-core-multi-agent` 从 `agent_core_multi_agent` 导入：

| API | 作用 |
| --- | --- |
| `AgentDescriptor` / `AgentTask` / `AgentResult` | Agent 声明、任务和统一结果 |
| `HandoffRequest` / `CoordinationExecution` | 任务交接和可恢复协调实例 |
| `AgentInvoker` / `AgentRouter` / `CoordinationStore` | 自定义 Agent、路由和存储端口 |
| `AgentRegistry` | 能力发现、别名、权限过滤和 Agent 并发限制 |
| `CapabilityRouter` / `ModelAgentRouter` | 确定性能力路由和结构化模型路由 |
| `CallableAgentInvoker` / `RuntimeAgentInvoker` | 函数与现有 `AgentRuntime` 适配器 |
| `SupervisorAgent` / `CoordinationPolicy` | handoff 循环、预算、重试、并行、取消和恢复 |
| `WorkflowCoordinationStore` | 复用 Core Workflow Store 的持久化适配器 |

扩展包依赖 Core，Core 不反向依赖扩展包。业务 Agent 只需实现 `AgentInvoker`，或者用适配器包装现有
运行时。

## 存储与访问

关系型 `create_*_store` 工厂按环境选择 Memory、SQLite 或 PostgreSQL；
`create_vector_store` 单独选择 Memory、Chroma 或 pgvector。公共 Store 包括：

- `SessionStore` / `BaseSessionStore`；
- `ContextStore`；
- `RunStore` / `RuntimeStore` / `ApprovalStore`；
- `RunLeaseStore`；
- `WorkflowStore` / `WorkflowExecutionStore`；
- `ModelSelectionStore`；
- `MemoryStore` / `MemoryRecord` / `MemorySearchResult`；
- `AccessContext`。

端口类型位于 `agent_core.ports`，实现位于 `agent_core.storage`。
`create_memory_store` 按环境创建 `InMemoryMemoryStore`、`SqliteMemoryStore` 或
`PostgresMemoryStore`。

## Prompt

| API | 作用 |
| --- | --- |
| `PromptTemplate` | 安全文本模板、默认变量和 partial |
| `MessageTemplate` / `MessagesPlaceholder` | 聊天消息模板和历史消息插槽 |
| `ChatPromptTemplate` | 把多条消息模板渲染为 `list[Message]` |
| `PromptRegistry` / `PromptVersion` / `PromptEntry` | Prompt 版本、回滚和文件持久化 |

## ACP

`AcpBackend`、`AcpSession`、`AcpUpdate`、`AcpStdioServer` 和 `run_acp_stdio` 构成 ACP 适配面。

## 兼容性原则

- 优先导入 `agent_core` 顶层公共符号；
- `agent_core.<module>` 可用于实现自定义端口，但不要依赖下划线开头的成员；
- 应用通过 Protocol 和注册表扩展，避免替换 Core 私有状态；
- 发布升级前阅读变更日志，并对恢复中的 Run 和工作流做兼容性评估。
