"""压缩范围选择：尾部保留预算 + 工具配对平衡边界（纯函数，无 IO）。

这一层是整个压缩策略里最需要正确性的部分：一旦切点落在
"assistant 发了 tool_call，但它的 tool 结果还在切点之后"的位置，
被压头部就会留下没有结果的 tool_call、尾部会以孤儿 tool 消息开头，
两者都会让模型接口直接返回 400。因此这里用纯函数把边界算法固定下来，
不依赖网络即可充分单测。
"""

from __future__ import annotations

import json

from agent_core.compaction.types import (
    SUMMARY_CLOSE,
    SUMMARY_OPEN,
    SUMMARY_SOURCE,
)
from agent_core.protocol.messages import Message

# checkpoint 消息的固定前言：明确告诉模型这是既定背景，继续任务、不要回应
CHECKPOINT_PREAMBLE = (
    "这是系统自动生成的早期对话检查点，用于释放上下文空间。"
    "请把它当作既定背景直接使用，从其后的消息继续任务，"
    "不要重述、不要回应、也不要提及本检查点。"
)


def estimate_message_tokens(message: Message, *, chars_per_token: int = 4) -> int:
    """按字符粗估单条消息的 token 数（含工具调用参数）。"""
    chars = len(message.content or "")
    for call in message.tool_calls:
        chars += len(json.dumps(call, ensure_ascii=False))
    return max(chars // chars_per_token, 1)


def estimate_tokens(messages: list[Message], *, chars_per_token: int = 4) -> int:
    """按字符粗估一组消息的 token 数（用于尾部预算与"是否变小"判断）。"""
    return sum(
        estimate_message_tokens(m, chars_per_token=chars_per_token) for m in messages
    )


def balanced_cut(messages: list[Message], proposed_cut: int) -> int:
    """把初步切点前移到最近的"工具配对平衡边界"。

    平衡的含义：该位置之前所有 assistant 发起的 tool_call，都已经在该
    位置之前拿到对应 tool 结果（在途调用计数为 0）。这样切出的头部不会
    遗留无结果的 tool_call，尾部也不会以孤儿 tool 消息开头。

    Args:
        messages: 完整消息列表。
        proposed_cut: 按尾部保留预算算出的初步切点（头部条数）。

    Returns:
        修正后的切点，只可能前移、不会超过 proposed_cut；
        因此尾部保留量只会 ≥ 预算，不会多压。
    """
    limit = max(0, min(proposed_cut, len(messages)))
    in_flight = 0
    last_balanced = 0
    for index, message in enumerate(messages):
        if message.role == "assistant":
            in_flight += len(message.tool_calls)
        elif message.role == "tool":
            in_flight = max(0, in_flight - 1)

        # index + 1 表示"第 index+1 条之前"这个切口；计数归零即此处平衡
        if in_flight == 0:
            last_balanced = index + 1
        if index + 1 >= limit:
            break

    return min(last_balanced, limit)


def select_cut(
    messages: list[Message],
    *,
    retain_tokens: int,
    chars_per_token: int = 4,
) -> int:
    """从尾部倒序累加 token，返回可压缩头部的条数（切点）。

    Args:
        messages: 完整消息列表。
        retain_tokens: 尾部原文保留预算（窗口 × retain_ratio）。
        chars_per_token: 字符/token 估算比例。

    Returns:
        初步切点：messages[cut:] 逐字保留，messages[:cut] 待压成摘要。
        若整段历史都在保留预算内，返回 0（表示没有可压缩的头部）。
    """
    retained = 0
    for index in range(len(messages) - 1, -1, -1):
        retained += estimate_message_tokens(
            messages[index], chars_per_token=chars_per_token
        )
        if retained >= retain_tokens:
            return index
    return 0


def extract_previous_summary(messages: list[Message], cut: int) -> str | None:
    """抽取被压头部里已有的摘要正文（滚动合并的基线）。

    程序靠 ``<compacted-summary>`` 标签认出"上一轮的旧摘要"：它本身就是
    压缩产物，既不该被当成普通对话再压一遍，也不能拿来跟新摘要比大小。

    Args:
        messages: 完整消息列表。
        cut: 压缩边界，只在 messages[:cut] 里找旧摘要。

    Returns:
        旧摘要正文；头部没有旧摘要（首次压缩）时返回 None。
    """
    for message in messages[:cut]:
        text = message.content or ""
        start = text.find(SUMMARY_OPEN)
        end = text.find(SUMMARY_CLOSE)
        if start >= 0 and end > start:
            body = text[start + len(SUMMARY_OPEN) : end].strip()
            if body:
                return body
    return None


def build_checkpoint(summary_text: str) -> Message:
    """把摘要正文包装成可写回历史的 checkpoint 消息。

    用 user 角色而非 system：既满足"system 永远只有首条"的约束，
    又能借前言让模型把摘要当作既定背景而不是新任务。
    """
    return Message(
        role="user",
        name=SUMMARY_SOURCE,
        content=f"{CHECKPOINT_PREAMBLE}\n\n{SUMMARY_OPEN}\n{summary_text}\n{SUMMARY_CLOSE}",
    )


def rebuild(
    messages: list[Message],
    cut: int,
    checkpoint: Message,
) -> list[Message]:
    """用 checkpoint 替换头部：system + checkpoint + 尾部原文。"""
    system_messages = [m for m in messages if m.role == "system"]
    tail = [m for m in messages[cut:] if m.role != "system"]
    return [*system_messages, checkpoint, *tail]
