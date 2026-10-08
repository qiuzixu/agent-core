# Handwritten Agent Core Multi-Agent

`handwritten-agent-core-multi-agent` 是 Agent Core 的可选多 Agent 编排扩展。它不依赖
LangChain、LangGraph、AutoGen 或 CrewAI，直接复用 Core 的运行时、事件、访问上下文、Workflow Store
和 Run Lease。

```bash
pip install handwritten-agent-core-multi-agent
```

```python
from agent_core_multi_agent import (
    AgentDescriptor,
    AgentRegistry,
    AgentTask,
    CallableAgentInvoker,
    HandoffRequest,
    SupervisorAgent,
)

registry = AgentRegistry()


async def researcher(task):
    return "检索完成"


registry.register(
    AgentDescriptor(
        agent_id="researcher",
        description="检索资料",
        capabilities=frozenset({"research"}),
    ),
    CallableAgentInvoker("researcher", researcher),
)

supervisor = SupervisorAgent(registry)
execution = await supervisor.run(
    AgentTask(
        thread_id="thread-1",
        input="查询飞行规则",
        required_capability="research",
    )
)
print(execution.output)
```

具体应用可以用 `RuntimeAgentInvoker` 包装现有 `AgentRuntime`。`WorkflowCoordinationStore` 支持复用
Core 的内存、SQLite、PostgreSQL Workflow Store；多 Worker 部署时再注入对应的 `RunLeaseStore`。
每次路由、调用和 handoff 都会落 checkpoint，恢复时使用相同任务 ID 和幂等键。
