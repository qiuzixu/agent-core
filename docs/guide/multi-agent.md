# Multi-Agent 编排

Multi-Agent 能力由独立发行包 `handwritten-agent-core-multi-agent` 提供。Core 继续负责单 Agent 的
Loop、模型、工具、会话、审批、Workflow Store、Run Lease 和事件；扩展包只负责多个 Agent 之间的
发现、路由、调用、handoff 和协调恢复。

```bash
pip install handwritten-agent-core-multi-agent
```

## 注册与调用

每个 Agent 通过 `AgentDescriptor` 声明能力、别名、优先级、角色要求和最大并发数，通过
`AgentInvoker` 接受统一 `AgentTask` 并返回 `AgentResult`：

```python
from agent_core_multi_agent import (
    AgentDescriptor,
    AgentRegistry,
    AgentTask,
    CallableAgentInvoker,
    SupervisorAgent,
)


async def search(task: AgentTask) -> str:
    return f"已检索：{task.input}"


registry = AgentRegistry()
registry.register(
    AgentDescriptor(
        agent_id="researcher",
        description="检索并核对资料",
        capabilities=frozenset({"research"}),
        aliases=frozenset({"search"}),
        max_concurrency=4,
    ),
    CallableAgentInvoker("researcher", search),
)

supervisor = SupervisorAgent(registry)
execution = await supervisor.run(
    AgentTask(
        thread_id="thread-1",
        input="查询低空飞行规则",
        required_capability="research",
    )
)
```

`CapabilityRouter` 按显式 Agent、能力和优先级做确定性路由。`ModelAgentRouter` 只会把当前身份可访问
的候选 Agent 交给 `ModelAdapter`，再通过 JSON Schema 校验选择结果，模型不能选择未注册或无权
访问的 Agent。

## 接入现有 AgentRuntime

应用已有的单 Agent 不需要改写。使用 `RuntimeAgentInvoker` 包装即可：

```python
from agent_core_multi_agent import AgentDescriptor, AgentRegistry, RuntimeAgentInvoker

registry.register(
    AgentDescriptor(
        agent_id="route-agent",
        description="查询、规划和校验低空航线",
        capabilities=frozenset({"route-query", "route-plan", "route-validation"}),
    ),
    RuntimeAgentInvoker("route-agent", route_runtime),
)
```

适配器会透传用户、租户、thread、幂等键和多 Agent 上下文，并把子 Agent 的 `parent_run_id` 指向
协调实例或上一个子 Agent Run。需要由业务结果决定 handoff 时，可以注入 `result_mapper`，把
`RunContext` 转成带 `HandoffRequest` 的 `AgentResult`。协调实例恢复时，适配器会对已有 checkpoint
的中断 Run 调用 `AgentRuntime.resume()`，继续原 Run 而不是创建新的 Run。

## Handoff 与恢复

子 Agent 返回 `HandoffRequest` 后，Supervisor 会创建新的不可变子任务，继承访问身份和上下文，
记录 `parent_task_id`、`parent_run_id`、depth 和 lineage，再重新路由：

```python
return AgentResult(
    task_id=task.task_id,
    agent_id="researcher",
    status="completed",
    output="资料已准备",
    handoff=HandoffRequest(
        required_capability="write",
        input="根据检索资料生成报告",
        reason="检索阶段完成",
    ),
)
```

每次路由、调用开始、调用结果和 handoff 都先写入 `CoordinationStore`。进程重启后，`resume()` 会从
`current_task` 继续；已经持久化的 completed 结果不会再次调用，未完成任务继续使用原 task id 和
幂等键。`interrupted` 结果可以在外部条件满足后显式恢复。`AgentTask.context`、metadata、handoff
context 和结果 metadata 必须是 JSON 可序列化数据，所有存储后端执行相同校验。

## 存储与多 Worker

`WorkflowCoordinationStore` 直接复用 Core 的 Workflow Store：

```python
from agent_core import create_run_lease_store, create_workflow_execution_store
from agent_core_multi_agent import SupervisorAgent, WorkflowCoordinationStore

workflow_store = create_workflow_execution_store(
    "development",
    sqlite_path="./agent.db",
)
lease_store = create_run_lease_store(
    "development",
    sqlite_path="./agent.db",
)

supervisor = SupervisorAgent(
    registry,
    store=WorkflowCoordinationStore(workflow_store, require_access=True),
    lease_store=lease_store,
)
```

测试环境使用内存 Store，本地开发使用 SQLite，生产环境使用 PostgreSQL。生产环境应同时注入
PostgreSQL Workflow Store 和 Run Lease Store，并在启动时调用两者的 `initialize()`。租约负责
Worker 互斥和心跳；任务幂等键负责外部副作用去重，两者不能互相替代。

## 预算、并发与事件

`CoordinationPolicy` 控制以下限制：

- 最大 handoff 次数、Agent 调用次数和同一 Agent 访问次数；
- 单 Agent 超时、有限重试和指数退避；
- 并行任务数量；
- 可选总 Token 与费用预算；
- AccessContext 强制要求；
- Worker 租约和心跳时间。

`run_sequence()` 和 `run_parallel()` 用于执行多个独立协调任务。Agent 自身的
`max_concurrency` 由注册表单独限制。协调生命周期使用 Core `RunEvent`，可以同时接入
`CallbackManager` 和 `EventSink`；回调或事件接收器故障不会覆盖 Agent 结果。

扩展包不会动态生成 Agent，不允许模型提供 Python 类路径，也不共享未授权的会话或长期记忆。
业务应用仍负责决定可注册 Agent、可见能力、上下文内容和具体业务权限。
