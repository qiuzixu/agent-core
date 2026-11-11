# ACP 协议

ACP（Agent Client Protocol）通过 JSON-RPC 连接编辑器等客户端与 Agent。Core 提供
`AcpStdioServer` 和应用侧 `AcpBackend` 契约；业务 Agent 负责接入自己的运行时、会话和审批机制。

## 已实现的协议流程

| 方向 | 方法或通知 | 行为 |
| --- | --- | --- |
| Client → Agent | `initialize` | 校验协议版本 `1`，返回 Agent 信息和能力声明 |
| Client → Agent | `session/new` | 创建会话并关联 Backend 的业务线程 |
| Client → Agent | `session/load` | 声明 `loadSession=true` 时恢复会话，可选回放历史 |
| Client → Agent | `session/prompt` | 接收文本内容块，执行提示并返回 `stopReason` |
| Client → Agent | `session/cancel` | 取消运行中的提示和待处理权限请求 |
| Agent → Client | `session/update` | 推送用户历史、Agent 文本/思考及工具事件 |
| Agent → Client | `session/request_permission` | 请求客户端选择审批选项 |

`session/prompt` 当前只提取 `type="text"` 内容块；纯图片、音频或嵌入内容不会进入 Backend。
新会话返回空的 `modes` 和 `configOptions`，Core 尚未提供会话模式切换或配置项协商。

## 接入 Backend

```python
import asyncio

from agent_core import AcpCapabilities, AcpSession, AcpUpdate, run_acp_stdio


class MyBackend:
    agent_name = "my-agent"
    agent_version = "0.2.1"
    capabilities = AcpCapabilities(load_session=True)

    async def create_session(self, session_id, cwd, mcp_servers):
        return AcpSession(session_id=session_id, cwd=cwd)

    async def load_session(self, session_id, cwd, mcp_servers):
        # 在应用持久层校验并恢复该会话。
        return AcpSession(session_id=session_id, cwd=cwd)

    async def replay_session(self, session, emit):
        # 可选：按照应用持久层中的历史顺序回放。
        await emit(AcpUpdate.user_text("历史用户消息"))
        await emit(AcpUpdate.text("历史 Agent 回复"))

    async def prompt(self, session, text, emit, request_permission):
        await emit(AcpUpdate.text(text))
        return {"stopReason": "end_turn"}

    async def cancel(self, session):
        pass


asyncio.run(run_acp_stdio(MyBackend()))
```

`AcpCapabilities` 的 `load_session`、`image`、`audio`、`embedded_context`、`mcp_http`、`mcp_sse`
默认均为 `False`。未声明 `capabilities` 的旧 Backend 可用 `supports_load_session` 控制恢复能力。
声明 `load_session=True` 只开放 `session/load`；历史回放还需要实现可选的
`AcpSessionReplayer.replay_session(session, emit)`。图片、音频和嵌入内容尚未进入当前文本提示链路，
不应在现有 Backend 中声明支持。

## 工具事件与权限

Backend 通过 `emit(AcpUpdate(...))` 推送更新。`tool_started` 与 `tool_finished` 的 payload
应携带相同的 `toolCallId`，客户端才能关联并发工具的开始和结束；缺失 ID 时服务端的兜底值
不能保证两条事件对应。`AcpUpdate.user_text()` 用于会话恢复时的用户消息回放。

`request_permission(...)` 发送 `session/request_permission`，选项必须有非空 `optionId`。
服务端识别平铺的 `selectedOptionId` / `optionId`，也识别嵌套 `outcome` 中的同名字段；
取消、超时和未知选项均按拒绝处理。Backend 应将审批结果映射到应用自己的权限与审计流程。
客户端传入的 `mcpServers` 也必须经过应用信任配置校验，不能直接执行任意命令。

## 边界

Core 仅提供 stdio 传输。HTTP、WebSocket、进程管理、身份认证、客户端发现和持久化会话实现
由应用或宿主环境负责。`initialize` 的多媒体/MCP 传输字段是能力声明，并不自动实现对应处理链路。
