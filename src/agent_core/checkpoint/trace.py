"""全链路 trace 查询：按 thread_id 查所有 namespace 的 checkpoint 树。

对齐 LangGraph 的 (thread_id, checkpoint_ns) 模式，提供统一的全链路视图。
"""

from __future__ import annotations
import inspect

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

    namespaces_outcome = list_namespaces(thread_id)
    # 内置实现里 list_namespaces 有同步（memory/sqlite）与异步（postgres）两种，
    # 统一用 isawaitable 收窄，两种协议实现都兼容。
    namespaces_raw = (
        await namespaces_outcome
        if inspect.isawaitable(namespaces_outcome)
        else list(namespaces_outcome)
    )
    result: dict[str, Any] = {}
    for ns in namespaces_raw:
        versions_outcome = list_versions(thread_id, checkpoint_ns=ns)
        if not inspect.isawaitable(versions_outcome):
            raise TypeError("checkpointer.list_versions 必须返回可等待结果")
        versions = await versions_outcome
        latest = versions[-1] if versions else None
        result[ns or DEFAULT_CHECKPOINT_NS] = {
            "versions": len(versions),
            "latest": latest,
        }

    return {"thread_id": thread_id, "namespaces": result}
