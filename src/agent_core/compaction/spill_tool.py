"""让 Agent 回读被上下文瘦身截断的完整工具输出。"""

from __future__ import annotations

import logging
from typing import Any

from agent_core.compaction.spill import SpillStore

logger = logging.getLogger(__name__)


def build_spill_tool(spill_store: SpillStore) -> list[dict[str, Any]]:
    """构造与 ``ToolRegistry`` 兼容的 spill 回读工具定义。"""

    async def read_spilled_content(spill_id: str) -> str:
        content = spill_store.get(spill_id)
        if content is None:
            return f"存档 {spill_id} 不存在或已过期。"
        logger.info("[Spill] 回读存档 %s（%d 字符）", spill_id, len(content))
        return content

    return [
        {
            "name": "read_spilled_content",
            "func": read_spilled_content,
            "description": "回读被瘦身截断的完整工具输出。消息中存在 spill_id 时可调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "spill_id": {
                        "type": "string",
                        "description": "瘦身标记中的 spill 存档 ID",
                    }
                },
                "required": ["spill_id"],
            },
        }
    ]


__all__ = ["build_spill_tool"]
