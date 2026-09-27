# 与 LangChain 生态对照

本文档把 **langchain-core**（抽象层）和 **LangGraph**（运行时层）的公开功能逐一列出，
与 **agent-core** 已提供的对应物做对照。便于评估哪些能力已经覆盖、哪些仍是空白。

> 信息来源：
> - langchain-core API Reference（[reference.langchain.com](https://reference.langchain.com/python/langchain-core)）
> - LangGraph 官方文档（[docs.langchain.com/oss/python/langgraph](https://docs.langchain.com/oss/python/langgraph)）

---

## 一、langchain-core（抽象层，无第三方集成）

| 模块 | 功能说明 | agent-core 对应物 | 状态 |
|---|---|---|---|
| **messages** | `BaseMessage`、`HumanMessage`、`AIMessage`、`SystemMessage`、`ToolMessage`；`trim_messages`、`filter_messages`、`merge_message_runs`、`convert_to_openai_messages` | `Message` + `system_message()` / `user_message()` / `assistant_message()` / `tool_message()`；压缩走 `CompactionMiddleware` | 已覆盖 |
| **prompts** | `ChatPromptTemplate`、`MessagesPlaceholder`；f-string / mustache / jinja2 模板；partial 变量 | `PromptTemplate`、`ChatPromptTemplate`、`MessagesPlaceholder`、partial、默认变量和版本注册表 | 已覆盖常用场景（不执行 mustache / jinja2） |
| **runnables (LCEL)** | `Runnable` 协议、`pipe`、并行执行、fallback、retry、batch、`with_config`、`astream_events` | 无 LCEL；用显式 `Middleware` 链替代 | 空白 |
| **chat_models** | `BaseChatModel` 统一接口、`init_chat_model`、bind_tools、结构化输出、rate limiter | `ModelAdapter`、`create_model_provider()`、`StructuredOutputSpec`、`GovernedModelAdapter` | 已覆盖 |
| **tools** | `@tool` 装饰器、`BaseTool`、`InjectedToolArg`、`StructuredTool` | `ToolSpec` / `ToolRegistry` / `ToolExecutor` | 已覆盖 |
| **output_parsers** | JSON / XML / Pydantic / 逗号列表等解析器 | JSON 提取、JSON Schema 子集校验、decoder 和失败重试 | 部分覆盖（无 XML/Pydantic 专用解析器） |
| **vectorstores / retrievers / embeddings** | 接口定义 | Core 协议、EmbeddingRetriever、Memory/Chroma/pgvector；独立 OpenAI/Gemini/Ollama 扩展 | 已覆盖核心协议和首批适配器 |
| **documents** | `Document` 类型 | `Blob`、`Document`、`DocumentChunk`、`DocumentLocator` | 已覆盖 |
| **document loaders / text splitters** | 加载器协议和独立 splitter 包 | Core 协议和纯文本加载；独立 MinerU 扩展；递归字符切分 | 已覆盖基础能力 |
| **load / serialization** | LangChain 对象 JSON 序列化协议 | 类型白名单、Schema 版本、迁移注册表 | 已覆盖安全序列化核心 |
| **callbacks** | 回调系统（LangSmith 埋点基础） | `CallbackHandler`、`CallbackManager`，和 EventSink 共用 `RunEvent` | 已覆盖核心生命周期订阅 |
| **caches / rate_limiters** | LLM 结果缓存、限流 | 按 scope 的请求/输入 Token 滑动窗口限流、并发队列；无结果缓存 | 部分覆盖 |
| **utils** | env、HTML、字符串工具 | — | — |

---

## 二、LangGraph（运行时层）

| 功能 | 说明 | agent-core 对应物 | 状态 |
|---|---|---|---|
| **Graph API** | `StateGraph`、节点/边/条件路由、reducer、subgraph | `StateMachineBuilder` | 已覆盖（无 reducer / subgraph 概念） |
| **Functional API** | `@entrypoint` / `@task` 装饰器，普通 Python 写工作流 | 节点函数 + `StateMachineBuilder` + `DurableWorkflowRunner` | 已覆盖主要执行能力（API 风格不同） |
| **Persistence** | checkpointer：`InMemorySaver` / `SqliteSaver` / `PostgresSaver`；thread 隔离 | `Checkpointer` + Memory / SQLite / PG 实现 | 已覆盖 |
| **Time travel** | 历史 checkpoint 回放、fork 分支 | `TimeTravelCheckpointer` | 已覆盖 |
| **Human-in-the-loop** | `interrupt()` + `Command(resume=)`、审批 / 编辑状态 | `ApprovalQueue` + `HumanInTheLoopMiddleware` | 已覆盖 |
| **Streaming** | values / updates / messages / debug 多种流模式、token 级流 | `RunEvent` 流 | 已覆盖（模式分类较少） |
| **Durable execution** | 失败从最近 checkpoint 恢复、pending writes | 节点执行前保存 `workflow_step_pending`，完成后保存状态与下一节点 | 已覆盖 |
| **Memory** | short-term（thread 内）+ long-term（Store 跨线程） | `SessionStore`（短期）+ `MemoryStore`（跨会话、TTL、版本、隔离、关键词检索） | 已覆盖通用存储端口（语义检索需适配器） |
| **Retry policy / caching** | 节点级重试策略、任务结果缓存 | `NodeExecutionPolicy`（超时、退避、异常范围、幂等约束）；无节点结果缓存 | 部分覆盖 |
| **prebuilt** | `create_react_agent`、`ToolNode` 开箱即用 | `ReActAgent` | 已覆盖 |
| **Multi-agent** | supervisor / swarm 多智能体编排 | 独立扩展提供注册表、规则/模型路由、handoff、父子 Run、预算、持久化恢复和租约 | 已覆盖 supervisor 与显式 handoff；无动态 swarm 群聊 |
| **图可视化** | `get_graph().draw_mermaid()` | `StateMachine.describe()` / `to_mermaid()` | 已覆盖 Mermaid |

---

## 三、仍存在的能力差异

以下按优先级排列：

1. **更高阶 Multi-agent 模式**
   - `handwritten-agent-core-multi-agent` 已提供 supervisor、显式 handoff、顺序/并行协调和恢复
   - 动态 swarm、群聊、投票/共识和运行时生成 Agent 尚未提供

2. **结果缓存**
   - 当前已有模型限流、并发、熔断和 fallback，但没有模型响应缓存或工作流节点结果缓存

3. **LCEL 编程模型**
   - Core 采用显式 Python 组装和 Middleware，没有 Runnable 管道 DSL

4. **第三方 Retrieval 适配器**
   - 尚未提供 Milvus、Qdrant 和通用 OCR
   - 这些实现应依赖 Core 协议，并作为可选扩展发布

5. **更多专用输出解析器**
   - JSON Schema 结构化输出已覆盖主路径，但 XML、CSV、Pydantic 等专用解析器仍由应用提供

这些差异不都需要照搬。Multi-agent 扩展已经明确共享上下文、授权、事件和失败语义；更复杂的
群聊或 swarm 应在出现真实业务需求后继续扩展。LCEL 属于编程模型选择，不是运行 Agent 的前置
条件。第三方文档和向量实现通过扩展适配器接入，避免改变 Core 的零运行时依赖属性。
