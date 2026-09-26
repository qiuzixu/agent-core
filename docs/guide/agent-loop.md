# Agent Loop 与运行生命周期

## ReAct 循环

`ReActAgent` 每轮把消息和工具定义发给模型。模型没有返回工具调用时，当前文本是最终答案；
有工具调用时，Core 并发执行本轮工具，把结果作为 `tool` 消息追加，然后进入下一轮。

```mermaid
sequenceDiagram
    participant App as 应用
    participant Loop as ReActAgent
    participant Model as ModelAdapter
    participant Tools as ToolExecutor
    App->>Loop: run(user_input)
    loop 直到得到最终答案
        Loop->>Model: chat(messages, tools)
        Model-->>Loop: assistant message
        alt 存在 tool_calls
            Loop->>Tools: execute_batch(tool_calls)
            Tools-->>Loop: ToolResult[]
            Loop->>Loop: 追加 tool messages
        else 无 tool_calls
            Loop-->>App: 最终文本
        end
    end
```

构造参数中的两个上限用于阻止失控循环：

- `max_iterations`：最多调用模型的轮数，默认 5；
- `max_tool_calls`：一次 Run 最多执行的工具数量，默认 20。

达到上限而模型仍请求工具时会抛出错误，不会把中间文本伪装成最终答案。

## 流式输出

`stream()` 在模型文本 chunk 到达时立即向调用方产出。工具调用需要收集完整参数后再执行，
因此工具调用边界不会产生不完整 JSON。普通执行和流式执行都保存等价的恢复状态。

```python
async for text in agent.stream("查询并解释结果"):
    await websocket.send_text(text)
```

上层协议负责把文本、工具事件和状态事件编码成 WebSocket、SSE 或其他传输格式。

## AgentRuntime

直接调用 `ReActAgent` 适合脚本和单进程任务。服务端应用应使用 `AgentRuntime`：

```python
run = await runtime.start(
    "thread-1",
    "执行任务",
    user_id="user-1",
    tenant_id="tenant-1",
    idempotency_key="request-123",
)
completed = await runtime.wait(run.context.thread_id, run.context.run_id)
```

Runtime 支持：

- 创建、等待、查询、取消和恢复 Run；
- 持久化 `RunContext` 与 `RunEvent`；
- 通过幂等键复用已有 Run；
- Worker 租约、心跳和过期运行恢复；
- 用户与租户归属检查；
- 把 Loop 的 checkpoint 回调持久化到 Run。

## 状态收口

Run 状态统一使用 `queued`、`running`、`interrupted`、`completed`、`failed`、`cancelled`。
模型、工具、中间件或持久化抛错时，Runtime 会尽可能保存失败状态并释放租约。

## 恢复语义

`resume(thread_id, run_id)` 加载持久化 Run 和 checkpoint，从保存的消息、迭代次数和工具调用
计数继续。使用多 Worker 时必须配置共享 `RunStore` 和 `RunLeaseStore`，避免同一 Run 被重复认领。
`recover_expired_runs()` 用于扫描已经失去有效租约的执行，可选择只列出或自动恢复。
