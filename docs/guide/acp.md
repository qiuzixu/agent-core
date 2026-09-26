# ACP 协议

ACP（Agent Client Protocol）允许编辑器或其他 Agent 客户端通过 JSON-RPC 与 Agent 服务通信。
Core 提供 stdio 服务端和应用后端 Protocol，不强制应用采用某个 Web 框架。

## 组件

- `AcpStdioServer`：读取 stdin 的 JSON-RPC 请求，并向 stdout 写响应和通知；
- `AcpBackend`：应用需要实现的会话、提示、取消接口；
- `AcpSession`：ACP 会话与业务线程的映射；
- `AcpUpdate`：文本或思考过程等标准化增量更新；
- `run_acp_stdio()`：启动服务端的便捷入口。

## 实现 Backend

```python
class MyBackend:
    agent_name = "my-agent"
    agent_version = "0.1.0"
    supports_load_session = True

    async def create_session(self, session_id, cwd, mcp_servers): ...
    async def load_session(self, session_id, cwd, mcp_servers): ...

    async def prompt(self, session, text, emit, request_permission):
        async for chunk in runtime.stream(session.session_id, text):
            await emit(AcpUpdate.text(chunk))
        return {"stopReason": "end_turn"}

    async def cancel(self, session): ...
```

启动：

```python
import asyncio

from agent_core import run_acp_stdio

asyncio.run(run_acp_stdio(MyBackend()))
```

## 权限与 MCP

ACP Client 可以在创建会话时传入 MCP Server 描述，也可以响应 Agent 发起的权限请求。
Backend 必须把这些协议对象映射到应用自己的受信配置、审批队列和访问上下文，不能直接执行
客户端提供的任意命令。

## 当前边界

Core 当前提供 stdio 传输。HTTP、WebSocket、进程管理、身份认证和 ACP 客户端发现机制由应用或
宿主环境负责。协议演进时应在 Backend 适配层处理兼容性，避免业务 Agent 直接依赖 JSON-RPC
字段细节。
