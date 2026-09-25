"""超长工具输出瘦身（P3，纯函数、无 IO）。

压缩是"整段历史 → 摘要"，粒度粗；但真正挤爆窗口的往往是几条超长工具
输出（Cesium 场景列表、航线 JSON、批量查询结果）。这些内容通常头部是
元信息、尾部是关键字段，中间是可再获取的明细，因此在摘要之前先做一层
保留式瘦身：头部 + 标记 + 尾部。

与摘要的区别：瘦身不生成新信息、不改写语义，只做截断，因此即便后续
摘要失败也可以单独采纳（见 CompactionMiddleware._compact）。

如果注入了 SpillStore，被截断的完整内容会存到进程内 KV 存，标记里
带上 spill_id；模型可通过 read_spilled_content 工具按 ID 回读全文，
而不是重新调一次业务接口。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agent_core.protocol.messages import Message

if TYPE_CHECKING:
    from agent_core.compaction.spill import SpillStore

# 被省略内容处的替换标记；显式写明省略量，避免模型误以为结果本就如此
PRUNE_MARKER = "\n\n…[工具输出已瘦身，中间省略 {dropped} 字符]…\n\n"
# spill 模式下的标记：多了 spill_id，指引模型用工具回读
PRUNE_MARKER_SPILL = (
    "\n\n…[工具输出已瘦身，中间省略 {dropped} 字符，"
    "完整内容可用 read_spilled_content 工具按 ID {spill_id} 回读]…\n\n"
)


@dataclass(frozen=True)
class PruneConfig:
    """工具输出瘦身阈值。

    Attributes:
        threshold_chars: 超过该长度的工具输出才瘦身。
        head_chars: 保留的头部字符数（元信息、字段说明）。
        tail_chars: 保留的尾部字符数（统计量、末尾条目）。
    """

    threshold_chars: int = 8192
    head_chars: int = 4096
    tail_chars: int = 1024

    def __post_init__(self) -> None:
        if self.threshold_chars <= 0:
            raise ValueError("prune threshold_chars 必须为正整数")
        if self.head_chars <= 0 or self.tail_chars < 0:
            raise ValueError("prune head_chars 必须为正、tail_chars 不能为负数")
        if self.head_chars + self.tail_chars >= self.threshold_chars:
            raise ValueError(
                "prune head_chars + tail_chars 必须小于 threshold_chars，"
                f"当前 {self.head_chars}+{self.tail_chars} >= {self.threshold_chars}"
            )


def prune_text(
    text: str,
    config: PruneConfig,
    *,
    spill_store: SpillStore | None = None,
) -> str:
    """按阈值截断单段文本；未超阈值时原样返回。

    如果注入了 spill_store，被截断的完整内容会存入 store，标记里带上
    spill_id 供模型回读；store 不接受（超限）时退回纯截断标记。
    """
    if len(text) <= config.threshold_chars:
        return text

    head = text[: config.head_chars]
    tail = text[len(text) - config.tail_chars :] if config.tail_chars else ""
    dropped = len(text) - len(head) - len(tail)
    if dropped <= 0:
        return text

    if spill_store is not None:
        spill_id = spill_store.put(text)
        if spill_id is not None:
            return f"{head}{PRUNE_MARKER_SPILL.format(dropped=dropped, spill_id=spill_id)}{tail}"

    return f"{head}{PRUNE_MARKER.format(dropped=dropped)}{tail}"


def prune_tool_results(
    messages: list[Message],
    *,
    config: PruneConfig,
    spill_store: SpillStore | None = None,
) -> tuple[list[Message], int]:
    """瘦身消息列表中的超长工具输出。

    只处理 role == "tool" 的消息，且仅在结果确实更短时才替换（返回新
    列表），否则原样返回入参列表，方便调用方用 ``is`` 判断"是否发生瘦身"。

    如果注入了 spill_store，被截断的完整内容会存入 store，标记里带上
    spill_id 供模型通过 read_spilled_content 工具回读。

    Returns:
        (消息列表, 被瘦身的条数)。
    """
    pruned_count = 0
    result: list[Message] = []
    for message in messages:
        if message.role != "tool" or not message.content:
            result.append(message)
            continue
        shorter = prune_text(message.content, config, spill_store=spill_store)
        if len(shorter) < len(message.content):
            result.append(replace(message, content=shorter))
            pruned_count += 1
        else:
            result.append(message)

    if pruned_count == 0:
        return messages, 0
    return result, pruned_count
