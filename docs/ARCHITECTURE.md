# Agent Core 架构

本文描述 `handwritten-agent-core` 自身的模块边界，以及应用 Agent 如何组装并调用 Core。
图中“应用层”由具体 Agent 项目实现；“Agent Core”不依赖 LangChain、LangGraph、FastAPI
或任何具体业务。

## 1. Agent Core 内部架构

```mermaid
flowchart TB
    PUBLIC["公共入口<br/>agent_core.__init__"]

    subgraph CONTRACTS["协议与边界"]
        PROTOCOL["protocol<br/>Message / RunContext / RunEvent<br/>ApprovalRecord / ToolResult"]
        ACCESS["access<br/>AccessContext"]
        PORTS["ports<br/>RunStore / SessionStore / ContextStore<br/>ApprovalStore / ModelSelectionStore / EventSink"]
        ERRORS["errors<br/>统一异常体系"]
    end

    subgraph ENGINE["执行引擎"]
        RUNTIME["AgentRuntime<br/>Run 生命周期 / 恢复 / 取消"]
        REACT["ReActAgent<br/>Agent Loop / 流式输出"]
        WORKFLOW["StateMachine<br/>DurableWorkflowRunner"]
        LEASE["Run Lease<br/>认领 / Heartbeat / 过期接管"]
    end

    subgraph EXTENSION["执行扩展链"]
        MIDDLEWARE["MiddlewareManager"]
        GUARDRAILS["Guardrails"]
        HITL["HITL<br/>持久化审批"]
        COMPACTION["Compaction<br/>摘要 / 裁剪 / Spill"]
        OBSERVABILITY["Observability<br/>统计 / Trace"]
    end

    subgraph IO["模型、工具与外部协议"]
        MODEL["ModelProviderRegistry<br/>ModelAdapter / Factory"]
        PROVIDERS["OpenAI / Anthropic<br/>Gemini / Ollama"]
        SKILLS["Skills<br/>SkillLoader / SkillRegistry<br/>SkillSpec / SkillActivation"]
        TOOLS["ToolRegistry<br/>ToolExecutor"]
        FUNCTIONS["本地 Function Calling"]
        MCP["McpToolClient<br/>MCP 工具发现与调用"]
        ACP["ACP Server<br/>JSON-RPC / stdio"]
    end

    subgraph STATE["状态与持久化"]
        SESSION["SessionManager<br/>多轮消息历史"]
        CONTEXT["ContextStore<br/>结构化长期上下文"]
        CHECKPOINT["Checkpointer<br/>版本 / 回滚 / 恢复"]
        PROMPTS["PromptRegistry<br/>Prompt 版本"]
        STORES["Storage Adapters<br/>Memory / SQLite / PostgreSQL"]
    end

    PUBLIC --> CONTRACTS
    PUBLIC --> ENGINE
    PUBLIC --> EXTENSION
    PUBLIC --> IO
    PUBLIC --> STATE

    RUNTIME --> REACT
    RUNTIME --> LEASE
    RUNTIME --> PORTS
    RUNTIME --> PROTOCOL
    REACT --> MIDDLEWARE
    REACT --> MODEL
    REACT --> TOOLS
    REACT --> CHECKPOINT
    WORKFLOW --> PORTS

    MIDDLEWARE --> GUARDRAILS
    MIDDLEWARE --> HITL
    MIDDLEWARE --> COMPACTION
    MIDDLEWARE --> OBSERVABILITY

    MODEL --> PROVIDERS
    SKILLS --> TOOLS
    SKILLS --> MCP
    TOOLS --> FUNCTIONS
    TOOLS --> MCP
    ACP --> RUNTIME

    SESSION --> PORTS
    CONTEXT --> PORTS
    CHECKPOINT --> PORTS
    PORTS --> STORES
    LEASE --> STORES
    ACCESS --> RUNTIME
    ACCESS --> SESSION
    ACCESS --> CONTEXT
    ACCESS --> HITL
    ERRORS --> ENGINE
    ERRORS --> IO
```

模块责任遵循以下原则：

- `protocol` 只定义跨模块传递的数据结构；
- `ports` 定义依赖接口及接口使用的值对象，具体数据库实现放在 `storage`，禁止反向依赖；
- `runtime` 负责一次 Run 和 Agent Loop，不包含任何业务判断；
- `skills` 负责加载和激活 Skill，应用显式提供本地 Function 和 MCP 客户端绑定；
- `tools` 统一调度本地函数和 MCP 工具，模型只看到统一的 Tool Schema；
- `middleware` 承载可组合的横切能力，例如审批、压缩、安全和可观测性；
- `workflow` 提供确定性状态机和节点级恢复，上层 Agent 定义具体业务节点；
- Memory 用于测试，SQLite 用于本地开发，PostgreSQL 用于生产多实例部署。

## 2. 应用 Agent 与 Agent Core 的调用关系

```mermaid
flowchart LR
    subgraph CLIENT["客户端"]
        WEB["Web React / CLI / ACP Client"]
    end

    subgraph APP["应用 Agent"]
        API["HTTP / SSE / WebSocket / ACP 适配层"]
        AUTH["认证与租户解析"]
        ASSEMBLY["Agent 组装<br/>配置 / Prompt / 模型 / 中间件"]
        BUSINESS["业务工作流与业务规则"]
        SKILLPACKS["Skill 包<br/>skill.json + SKILL.md"]
        LOCAL["业务 Function"]
        MCPCONFIG["MCP Server 配置"]
    end

    subgraph CORE["Agent Core"]
        ACCESSCTX["AccessContext"]
        SESSIONS["Session / Context"]
        AGENTRUNTIME["AgentRuntime"]
        LOOP["ReActAgent"]
        MODELFACTORY["Model Factory"]
        SKILLENGINE["SkillLoader / SkillRegistry<br/>加载 / 激活 / 绑定"]
        TOOLING["ToolRegistry / ToolExecutor"]
        MCPCLIENT["MCP Client"]
        APPROVAL["HITL / Workflow"]
        PERSIST["Run / Checkpoint / Lease / Stores"]
        EVENTS["RunEvent / EventSink"]
    end

    subgraph EXTERNAL["外部系统"]
        LLM["模型服务"]
        MCPSERVER["MCP Servers"]
        DBS["SQLite / PostgreSQL"]
        SERVICES["业务 API / 数据服务"]
    end

    WEB --> API
    API --> AUTH
    AUTH --> ACCESSCTX
    API --> ASSEMBLY
    ASSEMBLY --> BUSINESS
    ASSEMBLY --> SKILLENGINE
    ASSEMBLY --> MODELFACTORY
    ASSEMBLY --> TOOLING
    ASSEMBLY --> AGENTRUNTIME

    BUSINESS --> AGENTRUNTIME
    SKILLPACKS --> SKILLENGINE
    SKILLENGINE --> TOOLING
    LOCAL --> SKILLENGINE
    MCPCONFIG --> SKILLENGINE
    LOCAL --> TOOLING
    MCPCONFIG --> MCPCLIENT

    ACCESSCTX --> SESSIONS
    ACCESSCTX --> AGENTRUNTIME
    AGENTRUNTIME --> SESSIONS
    AGENTRUNTIME --> LOOP
    LOOP --> MODELFACTORY
    LOOP --> TOOLING
    LOOP --> APPROVAL
    MODELFACTORY --> LLM
    TOOLING --> LOCAL
    TOOLING --> MCPCLIENT
    MCPCLIENT --> MCPSERVER
    LOCAL --> SERVICES
    AGENTRUNTIME --> PERSIST
    APPROVAL --> PERSIST
    PERSIST --> DBS
    AGENTRUNTIME --> EVENTS
    EVENTS --> API
    API --> WEB
```

应用 Agent 提供具体 Skill 包、Function 实现、MCP 客户端及允许激活的 Skill 集合。Core 的
`SkillLoader` 负责安全加载和校验清单，`SkillRegistry` 负责注册与选择，激活结果把合并后的
instructions 和 Tool Schema 交给 `ReActAgent`。清单不会动态导入 Python 代码。

## 3. 一次 Function Calling / MCP 调用时序

```mermaid
sequenceDiagram
    participant User as 用户
    participant UI as Web Client
    participant App as 应用 Agent API
    participant Session as SessionManager
    participant Runtime as AgentRuntime
    participant ReAct as ReActAgent
    participant Model as ModelAdapter
    participant Tools as ToolExecutor
    participant MCP as MCP Server
    participant Store as Runtime Store

    User->>UI: 发送消息
    UI->>App: 请求 + agent/model/thread
    App->>Session: 加载会话与长期上下文
    App->>Runtime: start/run/stream
    Runtime->>Store: 保存 Run 并认领 Lease
    Runtime->>ReAct: 执行 Agent Loop
    ReAct->>Model: 消息 + Tool Schema

    alt 模型直接回答
        Model-->>ReAct: 文本响应
    else 模型发起 Function Calling
        Model-->>ReAct: tool_calls
        ReAct->>Tools: 并发执行工具
        alt 本地 Function
            Tools->>App: 调用业务函数
            App-->>Tools: 结构化结果
        else MCP Tool
            Tools->>MCP: call_tool
            MCP-->>Tools: MCP 结果
        end
        Tools-->>ReAct: ToolResult
        ReAct->>Store: 保存完整 Checkpoint
        ReAct->>Model: 工具结果进入下一轮
        Model-->>ReAct: 最终文本
    end

    ReAct-->>Runtime: 完成 / 中断 / 失败
    Runtime->>Store: 保存终态并释放 Lease
    Runtime-->>App: RunEvent / 流式文本
    App-->>UI: SSE / WebSocket / HTTP 响应
    UI-->>User: 展示结果
```

## 4. 应用接入示例

```mermaid
flowchart LR
    WEB["Web / CLI / ACP Client"]
    SETTINGS["应用设置<br/>模型 / Skill / MCP 配置"]

    subgraph AGENT["应用 Agent"]
        API["应用 API"]
        BUSINESS["业务编排"]
        CORE["agent-core<br/>Runtime / ReAct / Model / Skills / Tools<br/>Memory / Checkpoint / HITL / ACP"]
        MCP["MCP Client"]
    end

    GW["MCP Server"]
    VIEWER["前端界面"]
    BUSINESSAPI["业务服务"]

    SETTINGS --> WEB
    WEB --> VIEWER
    WEB -->|HTTP / SSE / WebSocket| API
    WEB -->|WebSocket / SSE| GW

    API --> BUSINESS
    API --> CORE
    BUSINESS --> CORE
    CORE --> MCP
    BUSINESS --> BUSINESSAPI
    MCP --> GW

    GW --> VIEWER
```

应用 Agent 直接使用 Core 加载 Skill、注册工具、运行 Agent Loop；
业务层负责提示词、业务工具和业务数据，通过应用适配器把 Core 能力接入自己的协议。
两者读取同一套共享 Skill 清单和指令，但分别注入自己的 Function 与 MCP 客户端，
并分别运行应用 API 和 MCP Server，避免进程与端口冲突。

## 5. 架构图维护待办

- [ ] **持续任务，不关闭：** 每次修改 `agent-core/src/agent_core/**` 后，在同一提交中检查并更新本文。
- [ ] 模块、公共接口、调用方向、持久化对象、协议或应用接入关系变化时，更新对应 Mermaid 图。
- [ ] 即使图形关系没有变化，也要在下面的同步记录中说明已检查以及无需改图的原因。
- [x] 已实现 Core Skill 系统，并将应用层 Skill 包与 Core 加载、注册、激活职责分开。

### 同步记录

| 日期 | 代码基线 | 架构同步内容 |
| --- | --- | --- |
| 2026-09-26 | `08a5fa1` | 建立 Core 内部架构、应用调用关系、工具调用时序和应用接入拓扑。 |
| 2026-09-26 | 本次提交 | 新增 SkillLoader、SkillRegistry、SkillSpec 和 SkillActivation，并同步应用绑定关系。 |
| 2026-09-26 | 应用 Skill 接入 | 应用 Agent 共享 Skill 包，分别接入手写工具执行器和 LangChain StructuredTool。 |
| 2026-09-26 | Web MCP 动态配置 | Web 端可持久化、测试并重连内置或自定义 MCP 服务；同步浏览器与 Gateway 的连接关系。 |
| 2026-09-26 | Core 边界治理 | 模型选择值对象和唯一存储端口收口到 `ports`；`storage` 仅保留实现，并修复审批持久化与中间件导入环。 |
| 2026-09-27 | 文档站迁移 | VitePress 迁移至 Material for MkDocs；清理 node/pnpm 残留；同步记录补录。 |
