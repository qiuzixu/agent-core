# ACP 模块说明

本目录实现 Agent Client Protocol（ACP）的通用适配层。它不负责模型推理、Agent Loop、业务工具或
持久化，而是负责把一个已经存在的 Agent Runtime 暴露给编辑器、CLI 或其他 ACP Client。

## 1. ACP、MCP 和 Agent Runtime 的关系

```text
ACP Client（编辑器 / CLI）
        │
        │ ACP：客户端调用 Agent
        ▼
AcpStdioServer
        │
        │ AcpBackend：协议对象转换为应用调用
        ▼
应用 Agent Runtime（ReAct / LangGraph / 其他实现）
        │
        │ MCP：Agent 调用外部工具和资源
        ▼
MCP Server
```

- ACP 解决“外部客户端如何创建会话、发送提示、接收更新、审批和取消 Agent”。
- MCP 解决“Agent 如何发现并调用外部工具、资源和提示词”。
- `AcpBackend` 是两者之间的应用适配边界，不能把 ACP Client 提供的配置直接视为可信配置。

## 2. 文件职责

| 文件 | 职责 |
| --- | --- |
| `types.py` | 定义 `AcpSession`、`AcpUpdate`、回调类型和应用必须实现的 `AcpBackend` 协议 |
| `server.py` | 实现基于 stdin/stdout 的双向 JSON-RPC 服务端、方法分发、更新通知、审批和取消 |
| `__init__.py` | 统一导出公共 API |

Core 只负责协议和传输。会话存储、模型调用、工具执行、访问控制和业务审批都由应用 Backend 负责。

## 3. `types.py` 中的数据结构

### `JsonObject`

`dict[str, Any]` 的可读别名，用于描述 JSON 对象。它不负责运行时校验；服务端会在协议入口校验
关键字段，应用 Backend 仍需校验业务字段。

### `AcpSession`

ACP 会话与应用会话的映射：

```python
AcpSession(
    session_id="acp-session-id",
    cwd="E:/workspace",
    metadata={"thread_id": "application-thread-id"},
)
```

- `session_id` 是 ACP Client 后续请求使用的会话标识。
- `cwd` 是客户端声明的工作目录，仅是上下文输入，不能天然视为已授权目录。
- `metadata` 保存应用自己的 thread、用户、租户或运行参数映射。

`AcpSession` 本身不保存消息历史。历史是否持久化取决于具体 Backend。

### `AcpUpdate`

应用 Runtime 向协议层发送的标准化更新。Core 当前转换以下 `kind`：

| `kind` | ACP 更新 | 用途 |
| --- | --- | --- |
| `text` | `agent_message_chunk` | 可展示给用户的文本片段 |
| `thought` | `agent_thought_chunk` | 可选展示的运行过程说明 |
| `tool_started` | `tool_call` | 工具开始及输入 |
| `tool_finished` | `tool_call_update` | 工具结果和成功状态 |

`AcpUpdate.text()` 和 `AcpUpdate.thought()` 是常用构造方法。工具事件使用
`AcpUpdate(kind, payload)`，并应提供稳定的 `toolCallId`，使开始和结束事件能够对应。

### `AcpEmitter` 与 `AcpPermissionRequester`

它们是回调类型，不是未实现的函数：

- `AcpEmitter`：Backend 调用它，把增量更新交给协议服务端。
- `AcpPermissionRequester`：Backend 调用它，请 ACP Client 选择批准或拒绝。

### `AcpBackend`

`AcpBackend` 是 Python `Protocol`，采用结构化类型检查。实现类不必继承它，只要提供相同字段和
方法即可。

| 成员 | 责任 |
| --- | --- |
| `agent_name` / `agent_version` | 返回给 ACP Client 的 Agent 信息 |
| `supports_load_session` | 声明是否支持恢复会话 |
| `create_session()` | 创建应用会话，并返回 ACP 与应用会话的映射 |
| `load_session()` | 从持久化状态恢复会话 |
| `prompt()` | 调用真实 Agent，通过回调发送更新和请求审批 |
| `cancel()` | 取消该会话当前正在执行的任务或 Run |

`Protocol` 方法体中的 `...` 只是类型声明，运行时不会创建或调用 `AcpBackend` 实例。真正代码位于
各应用的 Backend 实现中。

## 4. `server.py` 的处理流程

### 读取和并发

`AcpStdioServer.run()` 从 stdin 逐行读取 JSON。每行必须是一个完整 JSON-RPC 对象。请求会创建独立
异步任务，因此不同会话可以并发；`_write_lock` 保证响应写入 stdout 时不会互相穿插。

同一个会话同时只能执行一个 `session/prompt`。服务结束或 stdin 关闭时，未完成任务会被取消并等待
收尾。

### 客户端调用 Agent 的方法

| 方法 | 行为 |
| --- | --- |
| `initialize` | 校验协议版本，返回 Agent 信息与能力声明 |
| `session/new` | 生成 session ID，调用 `backend.create_session()` |
| `session/load` | 在 Backend 声明支持时调用 `backend.load_session()` |
| `session/prompt` | 提取文本内容并调用 `backend.prompt()` |
| `session/cancel` | 调用 `backend.cancel()`，同时取消服务端 prompt 任务 |

### Agent 主动调用客户端

`session/update` 是不需要响应的 JSON-RPC notification。它用于发送文本、思考过程和工具状态。

`session/request_permission` 是反向 JSON-RPC request。服务端生成请求 ID，把等待结果的 Future 保存到
`_pending_client_requests`，然后等待客户端响应。默认等待 300 秒，超时按 `reject_once` 处理。

### 一次提示的完整时序

```text
Client -> initialize
Client -> session/new
Server -> Backend.create_session()

Client -> session/prompt
Server -> Backend.prompt()
Backend -> emit(AcpUpdate)
Server -> session/update -> Client

Backend -> request_permission()             # 可选
Server -> session/request_permission -> Client
Client -> 批准或拒绝结果 -> Server
Server -> 审批结果 -> Backend

Backend -> {"stopReason": "end_turn"}
Server -> session/prompt 最终响应 -> Client
```

### 错误映射

| 场景 | JSON-RPC code |
| --- | ---: |
| JSON 解析失败 | `-32700` |
| 请求对象或 method 无效 | `-32600` |
| 参数或协议内容无效 | `-32602` |
| 方法或能力未实现 | `-32601` |
| 会话不存在 | `-32004` |
| Agent 执行异常 | `-32603` |

## 5. 新 Agent 如何接入

应用创建一个实现 `AcpBackend` 形状的类，在 `prompt()` 中调用自己的 Runtime：

```python
class MyAcpBackend:
    agent_name = "my-agent"
    agent_version = "0.1.0"
    supports_load_session = True

    async def create_session(self, session_id, cwd, mcp_servers):
        thread_id = await self.runtime.create_thread(session_id)
        return AcpSession(session_id, cwd, {"thread_id": thread_id})

    async def load_session(self, session_id, cwd, mcp_servers):
        await self.runtime.load_thread(session_id)
        return AcpSession(session_id, cwd, {"thread_id": session_id})

    async def prompt(self, session, text, emit, request_permission):
        async for chunk in self.runtime.stream(session.metadata["thread_id"], text):
            await emit(AcpUpdate.text(chunk))
        return {"stopReason": "end_turn"}

    async def cancel(self, session):
        await self.runtime.cancel(session.metadata["thread_id"])
```

然后使用：

```python
await run_acp_stdio(MyAcpBackend(runtime))
```

这是示意代码。生产实现还需要接入访问身份、租户、持久化、审批、幂等和安全的 MCP 配置白名单。

## 6. 两个低空 Agent 的当前接入情况

### Vanilla Agent

`VanillaAcpBackend` 把 ACP `session_id` 直接映射为 `RuntimeCompat` 的 `thread_id`，创建真实 Run，
轮询并转发模型、工具和审批事件。它支持持久化会话恢复；用户作出 ACP 审批决定后，通过恢复命令
创建后续 Run。取消操作会调用 Runtime 的 `cancel_run()`。

ACP Client 传入的 MCP 配置不会覆盖系统配置的 Gateway。

### LangGraph Agent

`LangGraphAcpBackend` 当前把历史保存在 ACP 进程内，调用现有图的 `ainvoke()`，发送一条过程说明和
最终回答。它声明 `supports_load_session=False`，因此不承诺进程重启后的 ACP 会话恢复；取消操作通过
取消当前图任务完成。

当前 LangGraph Backend 尚未完整转发图内部的工具事件和人工审批事件，能力范围低于 Vanilla Backend。

## 7. 空方法和占位实现审计

截至 2026-09-28，ACP 相关代码没有漏写的具体方法：

| 位置 | 表现 | 结论 |
| --- | --- | --- |
| `types.py` 的 `AcpBackend` | 4 个方法体使用 `...` | `Protocol` 接口声明，属于正确写法 |
| `server.py` 的未知方法分支 | 抛出 `NotImplementedError` | 映射为 JSON-RPC `-32601`，不是空实现 |
| `server.py` 的 `session/load` 检查 | Backend 不支持时抛出 `NotImplementedError` | 按能力声明拒绝请求 |
| `VanillaAcpBackend` | 没有 `pass`、`...` 或占位方法 | 已实现全部 Backend 方法 |
| `LangGraphAcpBackend.load_session()` | 明确抛出 `NotImplementedError` | 与 `supports_load_session=False` 一致，属于关闭的可选能力 |

因此不应把 `AcpBackend` 中的 `...` 改成业务实现。Core 只定义端口，具体实现必须继续留在应用 Agent。

## 8. 当前边界

- 仅提供 stdio 传输，没有内置 HTTP 或 WebSocket ACP 服务。
- 当前只接受文本 prompt；初始化能力中图片、音频和嵌入上下文均为 `False`。
- `mcpServers` 会传给 Backend，但 Core 不负责启动、认证或信任客户端声明的 MCP Server。
- Core 不负责用户认证、租户授权或工作目录授权，Backend 必须建立可信访问上下文。
- 服务端 `_sessions` 是当前 ACP 进程的索引；跨进程恢复取决于 Backend 和持久化 Runtime。
- 未识别的 `AcpUpdate.kind` 会被忽略。
- Core 支持增量更新，但是否真正逐块输出取决于 Backend 是否持续调用 `emit()`。
- 工具开始和结束事件必须使用同一个 `toolCallId`，否则客户端无法可靠关联。
