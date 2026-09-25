"""压缩摘要调用和结构校验。"""

from __future__ import annotations

import logging

from agent_core.compaction.prompts import (
    DEFAULT_COMPACTION_INSTRUCTION,
    DEFAULT_SUMMARY_SECTIONS,
)
from agent_core.compaction.types import CompactionError
from agent_core.model import ModelAdapter
from agent_core.protocol.messages import Message, user_message

logger = logging.getLogger(__name__)


def build_summarization_messages(
    messages: list[Message],
    *,
    cut: int,
    instruction: str = DEFAULT_COMPACTION_INSTRUCTION,
) -> list[Message]:
    """构造摘要调用的输入：原 system + 被压头部 + 末尾压缩指令。

    Args:
        messages: 压缩前的完整消息列表。
        cut: 压缩边界，messages[:cut] 为待压缩的头部。

    Returns:
        用于摘要模型的完整消息列表（system 仍在首位）。
    """
    head = messages[:cut]
    system_messages = [m for m in head if m.role == "system"]
    body = [m for m in head if m.role != "system"]
    return [*system_messages, *body, user_message(instruction)]


def normalize_summary(
    text: str,
    *,
    sections: tuple[str, ...] = DEFAULT_SUMMARY_SECTIONS,
) -> str:
    """校验并规整摘要结构。

    - 一个固定小节都没有 → 返回空串（调用方按失败处理，避免写入无效检查点）；
    - 只缺部分小节 → 用 (none) 补齐，保证后续轮次的滚动合并有稳定结构。
    """
    body = (text or "").strip()
    if not body:
        return ""

    present = [name for name in sections if f"## {name}" in body]
    if not present:
        return ""

    missing = [name for name in sections if name not in present]
    if missing:
        body += "\n\n" + "\n\n".join(f"## {name}\n(none)" for name in missing)
    return body


async def summarize(
    model: ModelAdapter,
    messages: list[Message],
    *,
    cut: int,
    max_tokens: int,
    instruction: str = DEFAULT_COMPACTION_INSTRUCTION,
    sections: tuple[str, ...] = DEFAULT_SUMMARY_SECTIONS,
) -> str:
    """调用摘要模型，把头部旧消息压成结构化检查点文本。

    Args:
        model: 摘要模型（默认复用主模型，也可换成更便宜的小模型）。
        messages: 压缩前的完整消息列表。
        cut: 压缩边界。
        max_tokens: 摘要生成本身的输出上限。

    Returns:
        结构化摘要正文（不含 checkpoint 包装）。

    Raises:
        CompactionError: 调用失败或输出不符合结构要求。
    """
    prompt = build_summarization_messages(messages, cut=cut, instruction=instruction)
    try:
        response = await model.chat(prompt, max_tokens=max_tokens)
    except Exception as exc:
        raise CompactionError(f"摘要模型调用失败：{exc}") from exc

    summary = normalize_summary(response.content or "", sections=sections)
    if not summary:
        raise CompactionError("摘要输出不符合结构化要求")
    return summary
