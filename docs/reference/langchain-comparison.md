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
| **prompts** | `ChatPromptTemplate`、`MessagesPlaceholder`；f-string / mustache / jinja2 模板；partial 变量 | 无 | 空白 |
| **runnables (LCEL)** | `Runnable` 协议、`pipe`、并行执行、fallback、retry、batch、`with_config`、`astream_events` | 无 LCEL；用显式 `Middleware` 链替代 | 空白 |
| **chat_models** | `BaseChatModel` 统一接口、`init_chat_model`、bind_tools、结构化输出、rate limiter | `ModelAdapter` + `create_model_provider()` | 已覆盖 |
| **tools** | `@tool` 装饰器、`BaseTool`、`InjectedToolArg`、`StructuredTool` | `ToolSpec` / `ToolRegistry` / `ToolExecutor` | 已覆盖 |
| **output_parsers** | JSON / XML / Pydantic / 逗号列表等解析器 | 无（靠模型 JSON 模式直接输出） | 空白 |
| **vectorstores / retrievers / embeddings** | 接口定义 | 无（留给应用） | 空白 |
| **documents** | `Document` 类型 | 无 | 空白 |
| **load / serialization** | LangChain 对象 JSON 序列化协议 | 无 | 空白 |
| **callbacks** | 回调系统（LangSmith 埋点基础） | `ObservabilityMiddleware`（OTel 钩子） | 已覆盖（方式不同） |
| **caches / rate_limiters** | LLM 结果缓存、限流 | 无 | 空白 |
| **utils** | env、HTML、字符串工具 | — | — |

---

## 二、LangGraph（运行时层）

| 功能 | 说明 | agent-core 对应物 | 状态 |
|---|---|---|---|
| **Graph API** | `StateGraph`、节点/边/条件路由、reducer、subgraph | `StateMachineBuilder` | 已覆盖（无 reducer / subgraph 概念） |
| **Functional API** | `@entrypoint` / `@task` 装饰器，普通 Python 写工作流 | `DurableWorkflowRunner`（装饰器风格） | 已覆盖 |
| **Persistence** | checkpointer：`InMemorySaver` / `SqliteSaver` / `PostgresSaver`；thread 隔离 | `Checkpointer` + Memory / SQLite / PG 实现 | 已覆盖 |
| **Time travel** | 历史 checkpoint 回放、fork 分支 | `TimeTravelCheckpointer` | 已覆盖 |
| **Human-in-the-loop** | `interrupt()` + `Command(resume=)`、审批 / 编辑状态 | `ApprovalQueue` + `HumanInTheLoopMiddleware` | 已覆盖 |
| **Streaming** | values / updates / messages / debug 多种流模式、token 级流 | `RunEvent` 流 | 已覆盖（模式分类较少） |
| **Durable execution** | 失败从最近 checkpoint 恢复、pending writes | 有（pending writes 粒度较粗） | 已覆盖（粒度较粗） |
| **Memory** | short-term（thread 内）+ long-term（Store 跨线程） | `SessionStore`（短期）；无长期语义记忆 | 部分覆盖 |
| **Retry policy / caching** | 节点级重试策略、任务结果缓存 | `RetryMiddleware`（模型/工具级，非节点级） | 部分覆盖 |
| **prebuilt** | `create_react_agent`、`ToolNode` 开箱即用 | `ReActAgent` | 已覆盖 |
| **Multi-agent** | supervisor / swarm 多智能体编排 | 无 | 空白 |
| **图可视化** | `get_graph().draw_mermaid()` | 无 | 空白 |

---

## 三、明显空白（LangChain 有、agent-core 无）

以下按优先级排列：

1. **Prompt 模板系统**
   - LangChain 的 `ChatPromptTemplate`、`MessagesPlaceholder` 使用频率极高
   - agent-core 当前完全由应用自行拼接 system prompt

2. **输出解析器**
   - JSON、XML、Pydantic、逗号列表等后处理解析器
   - 当前靠模型 `response_format={"type": "json_object"}` 兜底

3. **检索 / RAG 抽象**
   - retriever 接口、vectorstore 接口、embedding 接口
   - agent-core 不做向量检索抽象，留给应用选框架

4. **Multi-agent 编排**
   - supervisor 模式、swarm 模式、agent-to-agent 通信
   - 当前只有一个 `ReActAgent`，多 agent 需要应用自己协调

5. **长期语义记忆**
   - 跨会话的语义存储 + 检索（LangGraph 的 `Store`）
   - 当前 `SessionStore` 只覆盖单 session

6. **LLM 结果缓存 / 限流**
   - 相同 prompt 结果复用、并发请求限流

7. **图可视化导出**
   - 把状态机导出为 Mermaid / DOT 等格式，便于文档和调试
