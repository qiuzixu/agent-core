"""让 Agent 回读被上下文瘦身截断的完整工具输出。

支持三种读取方式，模型按需选择，避免一次性把巨型内容拉进上下文：
- 全文回读（不传 offset/limit/search）：适用于小内容或确定需要全文的场景；
- 分页回读（offset + limit）：适用于"看第 N 条"、"翻到下一页"等场景；
- 关键字搜索（search）：适用于"找包含 XX 的那条"等模糊定位场景。
"""

from __future__ import annotations

import logging
from typing import Any

from agent_core.compaction.spill import SpillStore

logger = logging.getLogger(__name__)

# 单次回读的默认最大字符数；超过时截断并提示模型用 offset/limit 分页
DEFAULT_PAGE_CHARS = 16 * 1024


def _paginate(content: str, *, offset: int, limit: int) -> tuple[str, int, int]:
    """按字符偏移量分页。

    Returns:
        (切片文本, 实际起始偏移, 剩余字符数)。
    """
    total = len(content)
    start = max(0, min(offset, total))
    end = min(start + limit, total)
    remaining = total - end
    return content[start:end], start, remaining


def _search_lines(content: str, query: str, *, max_matches: int = 20) -> str:
    """按行搜索包含 query 的行，返回带行号的匹配片段。

    如果没有匹配行，回退到在全文中做子串搜索（JSON 单行等情况）。
    """
    lines = content.splitlines()
    matches: list[str] = []
    for i, line in enumerate(lines, 1):
        if query.lower() in line.lower():
            matches.append(f"L{i}: {line.strip()}")
            if len(matches) >= max_matches:
                break

    if matches:
        return "\n".join(matches)

    # 单行 JSON 等无换行内容：回退到子串定位
    idx = content.lower().find(query.lower())
    if idx == -1:
        return ""
    context_start = max(0, idx - 200)
    context_end = min(len(content), idx + len(query) + 200)
    snippet = content[context_start:context_end]
    return f"[字符偏移 {idx} 附近] ...{snippet}..."


def build_spill_tool(spill_store: SpillStore) -> list[dict[str, Any]]:
    """构造与 ToolRegistry 兼容的 spill 回读工具定义（带分页和搜索）。"""

    async def read_spilled_content(
        spill_id: str,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_CHARS,
        search: str = "",
    ) -> str:
        content = spill_store.get(spill_id)
        if content is None:
            return f"存档 {spill_id} 不存在或已过期。"

        total_chars = len(content)

        # 搜索模式
        if search:
            result = _search_lines(content, search)
            if not result:
                logger.info("[Spill] 搜索存档 %s（%d 字符，关键词 %r）：无匹配", spill_id, total_chars, search)
                return f"存档 {spill_id}（共 {total_chars} 字符）中未找到包含 {search!r} 的内容。"
            logger.info("[Spill] 搜索存档 %s（%d 字符，关键词 %r）", spill_id, total_chars, search)
            return f"[存档 {spill_id} 共 {total_chars} 字符，搜索 {search!r} 结果]\n{result}"

        # 分页模式
        if offset > 0 or limit < total_chars:
            page, actual_start, remaining = _paginate(content, offset=offset, limit=limit)
            header = f"[存档 {spill_id} 共 {total_chars} 字符，显示 {actual_start}-{actual_start + len(page)}]"
            if remaining > 0:
                header += f"，剩余 {remaining} 字符（可用 offset={actual_start + len(page)} 继续）"
            logger.info("[Spill] 分页回读存档 %s（offset=%d limit=%d / %d 字符）", spill_id, actual_start, limit, total_chars)
            return f"{header}\n{page}"

        # 全文回读
        logger.info("[Spill] 全文回读存档 %s（%d 字符）", spill_id, total_chars)
        return content

    return [
        {
            "name": "read_spilled_content",
            "func": read_spilled_content,
            "description": (
                "回读被瘦身截断的完整工具输出。消息中存在 spill_id 时可调用。"
                "内容很大时建议传 offset/limit 分页读取，或传 search 关键字定位，避免一次性把全文拉进上下文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spill_id": {"type": "string", "description": "瘦身标记中的 spill 存档 ID"},
                    "offset": {"type": "integer", "description": "字符偏移量，从该位置开始读取（默认 0 = 从头开始）"},
                    "limit": {"type": "integer", "description": f"本次最多读取的字符数（默认 {DEFAULT_PAGE_CHARS}）"},
                    "search": {"type": "string", "description": "关键字搜索：返回匹配行或片段，不返回全文"},
                },
                "required": ["spill_id"],
            },
        }
    ]


__all__ = ["build_spill_tool", "DEFAULT_PAGE_CHARS"]
