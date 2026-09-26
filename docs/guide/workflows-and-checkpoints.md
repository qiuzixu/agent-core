# 工作流与 Checkpoint

Agent Loop 适合由模型动态决定下一步；状态机适合有固定流程、条件分支和明确恢复点的任务。
两者可以组合：工作流节点内部调用 Agent，Agent 的每轮状态再由 Checkpoint 保存。

## Checkpointer

`ReActAgent` 接受 `Checkpointer` Protocol：

```python
agent = ReActAgent(
    ...,
    checkpointer=TimeTravelCheckpointer("./checkpoints"),
    thread_id="thread-1",
)
```

内置实现：

| 实现 | 用途 |
| --- | --- |
| `MemoryCheckpointer` | 单元测试和临时执行 |
| `FileCheckpointer` | 本地开发的最新快照 |
| `TimeTravelCheckpointer` | 保留历史版本、查看和回滚 |

Checkpoint 保存消息、迭代次数、工具调用计数、最后响应和运行元数据。文件实现适合开发，
多进程生产服务应使用共享的 Run/Workflow 存储或自行实现共享 `Checkpointer`。

## 构建状态机

```python
machine = (
    StateMachineBuilder(max_steps=20)
    .add_node("prepare", prepare)
    .add_node("execute", execute)
    .add_edge(START, "prepare")
    .add_edge("prepare", "execute")
    .add_edge("execute", END)
    .build()
)
result = await machine.ainvoke({"request_id": "42"})
```

节点接收状态字典，返回需要合并的更新。条件边函数根据更新后的状态返回下一个节点名。
`max_steps` 防止错误路由导致无限循环。

## 持久化执行

`DurableWorkflowRunner` 在每个节点边界保存 `WorkflowExecution`：

```python
runner = DurableWorkflowRunner(
    machine,
    create_workflow_execution_store(
        "development",
        sqlite_path="./agent.db",
    ),
    require_access=True,
)

execution = await runner.start(
    "flight-plan",
    {"request_id": "42"},
    access=AccessContext(user_id="u-1", tenant_id="t-1"),
)
```

节点抛出 `WorkflowPause(reason, data)` 时，执行状态变为 `interrupted`，并保留当前节点。
外部条件满足后调用 `resume(execution_id, access=...)`，不会重复已经完成的节点。

完整示例见 `examples/durable_workflow.py`。

## 幂等与副作用

节点级持久化不能自动撤销已经完成的外部副作用。执行写操作时应使用业务幂等键，
并把远端操作标识写入状态后再进入下一节点。无法幂等的操作应在执行前请求审批，或使用业务
事务和补偿流程。
