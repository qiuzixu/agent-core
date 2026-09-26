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

## 状态、工作流与审批

| API | 作用 |
| --- | --- |
| `Checkpointer` / `MemoryCheckpointer` / `FileCheckpointer` | 最新快照协议和实现 |
| `TimeTravelCheckpointer` / `CheckpointVersion` | 历史版本与回滚 |
| `StateMachineBuilder` / `StateMachine` | 通用异步状态机 |
| `DurableWorkflowRunner` / `WorkflowPause` | 节点级持久化和中断恢复 |
| `ApprovalQueue` / `ApprovalRequest` / `ApprovalStatus` | 审批创建、等待和决策 |
| `ApprovalRecord` | 持久化审批值对象 |

## 存储与访问

所有 `create_*_store` 工厂按环境选择 Memory、SQLite 或 PostgreSQL。公共 Store 包括：

- `SessionStore` / `BaseSessionStore`；
- `ContextStore`；
- `RunStore` / `RuntimeStore` / `ApprovalStore`；
- `RunLeaseStore`；
- `WorkflowStore` / `WorkflowExecutionStore`；
- `ModelSelectionStore`；
- `AccessContext`。

端口类型位于 `agent_core.ports`，实现位于 `agent_core.storage`。

## ACP

`AcpBackend`、`AcpSession`、`AcpUpdate`、`AcpStdioServer` 和 `run_acp_stdio` 构成 ACP 适配面。

## 兼容性原则

- 优先导入 `agent_core` 顶层公共符号；
- `agent_core.<module>` 可用于实现自定义端口，但不要依赖下划线开头的成员；
- 应用通过 Protocol 和注册表扩展，避免替换 Core 私有状态；
- 发布升级前阅读变更日志，并对恢复中的 Run 和工作流做兼容性评估。
