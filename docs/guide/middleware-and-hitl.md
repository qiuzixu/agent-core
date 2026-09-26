# Middleware 与 HITL

Middleware 在模型和工具调用前后执行横切逻辑。Core 内置日志、重试、上下文限制、压缩、
Guardrails、可观测性和人工审批中间件。

## 生命周期钩子

自定义中间件继承 `Middleware`，按需覆盖：

```python
class AuditMiddleware(Middleware):
    async def before_tool(self, context):
        audit(context.tool_name, context.tool_args)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
```

钩子返回 `CONTINUE`、`MODIFY` 或 `STOP`。`MODIFY` 可替换消息等上下文，`STOP` 中断当前路径。
`handle_exception()` 可识别并处理允许重试或降级的异常。

中间件顺序会影响行为。推荐顺序如下：

```python
middleware = [
    GuardrailsMiddleware(...),
    CompactionMiddleware(model, tool_definitions=tool_definitions),
    TokenLimitMiddleware(max_tokens=120_000),
    HumanInTheLoopMiddleware(...),
    ObservabilityMiddleware(thread_id="thread-1"),
]
```

## 两层审批入口

Core 支持两种互补方式：

- `ToolSpec.requires_approval` + `ToolExecutor.approval_callback`：工具执行边界上的简洁审批；
- `HumanInTheLoopMiddleware`：在中间件链中按工具名暂停或调用审批回调。

审批回调签名为 `async (tool_name, args) -> bool`：

```python
async def approve(tool_name: str, arguments: dict) -> bool:
    request = await approval_queue.create_request_async(
        thread_id="thread-1",
        tool_name=tool_name,
        tool_args=arguments,
        user_id="user-1",
        tenant_id="tenant-1",
    )
    decision = await approval_queue.wait_for_decision(request, timeout_seconds=300)
    return decision is ApprovalStatus.APPROVED


hitl = HumanInTheLoopMiddleware(
    approval_needed=["delete_entity"],
    approval_callback=approve,
    timeout_seconds=300,
)
```

## 持久化审批

`ApprovalQueue` 可接入 `ApprovalStore`。配置存储后必须使用异步
`create_request_async()`，保证返回时初始记录已经落库。等待方会轮询共享存储，因此批准动作
可以来自另一个 API 进程。`approve()` 和 `reject()` 接受 `AccessContext` 做归属校验。

没有回调时，`HumanInTheLoopMiddleware` 返回 `STOP` 和 `approval_needed` 数据。上层应用负责
把 Run 标记为可恢复状态，并在用户决定后触发恢复。

## 自动批准

`auto_approve=True` 只适用于测试。生产环境不要用它绕过高风险工具审批。
