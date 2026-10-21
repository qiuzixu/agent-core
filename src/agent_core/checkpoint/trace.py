"""全链路 trace 查询：按 thread_id 查所有 namespace 的 checkpoint 树。

对齐 LangGraph 的 (thread_id, checkpoint_ns) 模式，提供统一的全链路视图。
"""

from __future__ import annotations

from typing import Any

from agent_core.checkpoint.store import Checkpointer, DEFAULT_CHECKPOINT_NS

__all__ = ["trace_tree"]


async def trace_tree(checkpointer: Checkpointer, thread_id: str) -> dict[str, Any]:
    """返回该 thread 下所有 namespace 的 checkpoint 树。

    Args:
        checkpointer: 支持 list_namespaces / list_versions 的 checkpoint 存储实例。
        thread_id: 会话 ID（对话和工作流共用同一个）。

    Returns:
        {
            "thread_id": "chat-abc-123",
            "namespaces": {
                "": {"versions": 3, "latest": "step3"},           ← 对话层
                "workflow:exec-001": {"versions": 5, "latest": "step5"},  ← workflow 层
            },
        }
    """
    list_namespaces = getattr(checkpointer, "list_namespaces", None)
    list_versions = getattr(checkpointer, "list_versions", None)
    if list_namespaces is None or list_versions is None:
        return {"thread_id": thread_id, "namespaces": {}, "error": "checkpointer 不支持 namespace 查询"}

    namespaces_raw = list_namespaces(thread_id)
    result: dict[str, Any] = {}
    for ns in namespaces_raw:
        versions = await list_versions(thread_id, checkpoint_ns=ns)
        latest = versions[-1] if versions else None
        result[ns or DEFAULT_CHECKPOINT_NS] = {
            "versions": len(versions),
            "latest": latest,
        }

    return {"thread_id": thread_id, "namespaces": result}
