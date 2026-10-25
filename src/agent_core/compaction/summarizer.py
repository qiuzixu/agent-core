"""压缩摘要调用和结构校验。"""

from __future__ import annotations

import logging

from agent_core.compaction.prompts import (
    DEFAULT_COMPACTION_INSTRUCTION,
    DEFAULT_SUMMARY_SECTIONS,
)
from agent_core.compaction.region import estimate_tokens
from agent_core.compaction.types import CompactionError
from agent_core.model import ModelAdapter, StreamChunk
from agent_core.protocol.messages import Message, assistant_message, user_message

logger = logging.getLogger(__name__)

# 各家模型"输出被截断"时 finish_reason 的常见取值（OpenAI 用 length，部分端点用 max_tokens）
_TRUNCATED_FINISH_REASONS = {"length", "max_tokens"}
# 适配器不上报 finish_reason 时的启发式阈值：估算 token 达到上限的 95% 视为疑似截断
_TRUNCATION_HEADROOM_RATIO = 0.95


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

    使用流式调用（stream_chat）而非一次性 chat：流式响应携带 finish_reason，
    可以识别"输出被截断"的情况。被截断的摘要一旦落库，会成为下一轮滚动
    合并的基线，关键状态会永久丢失，因此这里宁可失败重试也不写入残缺摘要。

    Args:
        model: 摘要模型（默认复用主模型，也可换成更便宜的小模型）。
        messages: 压缩前的完整消息列表。
        cut: 压缩边界。
        max_tokens: 摘要生成本身的输出上限。

    Returns:
        结构化摘要正文（不含 checkpoint 包装）。

    Raises:
        CompactionError: 调用失败、输出被截断、不符合结构要求时抛出。
            错误文本保留原始异常信息，中间件依赖它匹配"上下文超长"
            标记来触发收缩重试。
    """
    prompt = build_summarization_messages(messages, cut=cut, instruction=instruction)

    chunks: list[StreamChunk] = []
    try:
        async for chunk in model.stream_chat(prompt, max_tokens=max_tokens):
            chunks.append(chunk)
    except Exception as exc:
        # 保留原始错误文本：middleware.is_context_overflow_error 靠文本匹配
        # "context length" 等标记来决定是否收缩切点重试
        raise CompactionError(f"摘要模型调用失败：{exc}") from exc

    text_parts = [chunk.text or "" for chunk in chunks]
    raw_text = "".join(text_parts)

    # 防截断检查 1：finish_reason 明确表示输出被截断
    finish_reasons = [c.finish_reason for c in chunks if c.finish_reason is not None]
    finish_reason = finish_reasons[-1] if finish_reasons else None
    if finish_reason in _TRUNCATED_FINISH_REASONS:
        raise CompactionError(
            f"摘要输出被截断（finish_reason={finish_reason}），拒绝写入残缺检查点"
        )

    # 防截断检查 2：流中出现了工具调用，说明模型没有按摘要指令输出
    if any(c.tool_calls or c.is_tool_call for c in chunks):
        raise CompactionError("摘要输出包含工具调用，不符合摘要预期，拒绝写入")

    # 防截断检查 3：适配器不上报 finish_reason 时的启发式兜底——
    # 估算 token 逼近上限即视为疑似截断
    if finish_reason is None and max_tokens > 0:
        estimated = estimate_tokens([assistant_message(raw_text)])
        if estimated >= int(max_tokens * _TRUNCATION_HEADROOM_RATIO):
            raise CompactionError(
                f"摘要输出疑似被截断（估算 {estimated} tokens ≥ 上限 {max_tokens} "
                f"的 95%），拒绝写入残缺检查点"
            )

    summary = normalize_summary(raw_text, sections=sections)
    if not summary:
        raise CompactionError("摘要输出不符合结构化要求")
    return summary
