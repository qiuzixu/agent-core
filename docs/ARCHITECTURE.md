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
        PORTS["ports<br/>RunStore / SessionStore / ContextStore<br/>ApprovalStore / EventSink"]
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
- `ports` 只定义依赖接口，具体数据库实现放在 `storage`；
- `runtime` 负责一次 Run 和 Agent Loop，不包含 Cesium、航线等业务判断；
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
        SKILLS["Skill 组合层<br/>提示词 + 工具集合 + 权限"]
        LOCAL["业务 Function"]
        MCPCONFIG["MCP Server 配置"]
    end

    subgraph CORE["Agent Core"]
        ACCESSCTX["AccessContext"]
        SESSIONS["Session / Context"]
        AGENTRUNTIME["AgentRuntime"]
        LOOP["ReActAgent"]
        MODELFACTORY["Model Factory"]
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
    ASSEMBLY --> SKILLS
    ASSEMBLY --> MODELFACTORY
    ASSEMBLY --> TOOLING
    ASSEMBLY --> AGENTRUNTIME

    BUSINESS --> AGENTRUNTIME
    SKILLS --> TOOLING
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

这里的 `Skill 组合层` 目前属于应用 Agent：一个 Skill 可以组合提示词、本地 Function、MCP Tool、
权限和审批策略。Core 已经提供其底层组成能力，但尚未实现一等公民的 `SkillSpec`、
`SkillRegistry` 和动态加载器。

## 3. 一次 Function Calling / MCP 调用时序

```mermaid
sequenceDiagram
    actor User as 用户
    participant UI as Web/Client
    participant App as 应用 Agent API
    participant Session as SessionManager
    participant Runtime as AgentRuntime
    participant Loop as ReActAgent
    participant Model as ModelAdapter
    participant Tools as ToolExecutor
    participant MCP as MCP Server
    participant Store as Runtime/Checkpoint Store

    User->>UI: 发送消息
    UI->>App: 请求 + agent/model/thread
    App->>Session: 加载会话与长期上下文
    App->>Runtime: start/run/stream
    Runtime->>Store: 保存 Run 并认领 Lease
    Runtime->>Loop: 执行 Agent Loop
    Loop->>Model: 消息 + Tool Schema

    alt 模型直接回答
        Model-->>Loop: 文本响应
    else 模型发起 Function Calling
        Model-->>Loop: tool_calls
        Loop->>Tools: 并发执行工具
        alt 本地 Function
            Tools->>App: 调用业务函数
            App-->>Tools: 结构化结果
        else MCP Tool
            Tools->>MCP: call_tool
            MCP-->>Tools: MCP 结果
        end
        Tools-->>Loop: ToolResult
        Loop->>Store: 保存完整 Checkpoint
        Loop->>Model: 工具结果进入下一轮
        Model-->>Loop: 最终文本
    end

    Loop-->>Runtime: 完成 / 中断 / 失败
    Runtime->>Store: 保存终态并释放 Lease
    Runtime-->>App: RunEvent / 流式文本
    App-->>UI: SSE / WebSocket / HTTP 响应
    UI-->>User: 展示结果
```

## 4. 当前项目接入拓扑

```mermaid
flowchart LR
    WEB["web-app-react<br/>Agent 与模型切换"]

    subgraph LANGGRAPH["my-cesium-agent :2024"]
        LGAPI["应用 API"]
        LGFLOW["LangGraph 业务工作流"]
        LGACP["agent-core ACP 适配"]
        LGMCP["Cesium MCP Client"]
    end

    subgraph VANILLA["my-cesium-agent-vanilla :2025"]
        VAPI["应用 API"]
        VBUSINESS["低空业务编排"]
        VCORE["agent-core<br/>Runtime / ReAct / Model / Tools<br/>Memory / Checkpoint / HITL / ACP"]
        VMCP["agent-core MCP Client"]
    end

    GW1["Cesium MCP Gateway :3010"]
    GW2["Cesium MCP Gateway :3011"]
    VIEWER["CesiumJS Viewer"]
    BUSINESSAPI["低空业务服务"]

    WEB -->|选择 LangGraph Agent| LGAPI
    WEB -->|选择 Vanilla Agent| VAPI

    LGAPI --> LGFLOW
    LGAPI --> LGACP
    LGFLOW --> LGMCP
    LGFLOW --> BUSINESSAPI
    LGMCP --> GW1

    VAPI --> VBUSINESS
    VAPI --> VCORE
    VBUSINESS --> VCORE
    VCORE --> VMCP
    VBUSINESS --> BUSINESSAPI
    VMCP --> GW2

    GW1 --> VIEWER
    GW2 --> VIEWER
```

当前 `my-cesium-agent-vanilla` 深度复用 Agent Core；`my-cesium-agent` 仍以 LangGraph
工作流为主，目前主要复用 Core 的 ACP 协议能力。两者共享前端交互契约，但分别运行自己的 Agent
API 和 Cesium MCP Gateway，避免进程与端口冲突。

## 5. 架构图维护待办

- [ ] **持续任务，不关闭：** 每次修改 `agent-core/src/agent_core/**` 后，在同一提交中检查并更新本文。
- [ ] 模块、公共接口、调用方向、持久化对象、协议或应用接入关系变化时，更新对应 Mermaid 图。
- [ ] 即使图形关系没有变化，也要在下面的同步记录中说明已检查以及无需改图的原因。
- [ ] 正式实现 Skill 系统后，把图中的应用层 Skill 组合升级为 Core 的 `SkillSpec`、
  `SkillRegistry`、加载器和执行策略。

### 同步记录

| 日期 | 代码基线 | 架构同步内容 |
| --- | --- | --- |
| 2026-09-26 | `08a5fa1` | 建立 Core 内部架构、应用调用关系、工具调用时序和双 Agent 接入拓扑。 |
