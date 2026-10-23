"""Agent 运行时层。

本模块包含三层职责：

1. react.py：手写 ReAct Agent 循环，负责模型调用、工具调用、checkpoint
   恢复/保存、中间件执行和 run/stream 两种对外路径。
2. service.py：AgentRuntime 生命周期门面，负责启动、恢复、取消、
   Worker 租约、事件发布和状态持久化。
3. compat.py / store.py：HTTP/API 适配层常用的运行摘要、消息解析，
   以及测试和最小开发环境使用的内存 RunStore。

应用层通常先构造 ReActAgent，再交给 AgentRuntime 管理；普通请求使用
start / wait，流式请求直接使用 AgentRuntime.stream。
"""

from agent_core.runtime.compat import (
    RunRecord,
    ThreadRecord,
    ThreadStatus,
    content_text,
    extract_prompt,
    message_json,
    messages_json,
    now_iso,
)
from agent_core.runtime.react import ReActAgent
from agent_core.runtime.service import AgentRuntime, MemoryEventSink, RunExecutor, RuntimeRun
from agent_core.runtime.state import (
    RunStateError,
    can_transition_run_status,
    transition_run_status,
)
from agent_core.runtime.store import MemoryRunStore

__all__ = [
    "AgentRuntime",
    "MemoryEventSink",
    "MemoryRunStore",
    "ReActAgent",
    "RunExecutor",
    "RunStateError",
    "RunRecord",
    "RuntimeRun",
    "ThreadRecord",
    "ThreadStatus",
    "can_transition_run_status",
    "content_text",
    "extract_prompt",
    "message_json",
    "messages_json",
    "now_iso",
    "transition_run_status",
]
