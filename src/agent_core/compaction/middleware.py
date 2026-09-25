"""CompactionMiddleware：窗口压力下的自动上下文压缩。

挂在 before_model 上，每次模型调用前做一次判断：

    计数 → 未达阈值放行 → 达阈值则先瘦身超长工具输出、再把头部旧消息
    压成结构化检查点 → MODIFY

另外实现 handle_exception：模型侧真报"上下文超长"时（计数低估、工具
schema 变大等情况），无视阈值强制压一轮并让循环重试。

设计基调与项目一致：主路径优雅，失败有兜底。任何一步出问题（计数不可用、
摘要失败、结构不合规、摘要没变小）都只记录日志并返回 CONTINUE，
让后续的 TokenLimitMiddleware 继续执行硬截断，主流程与用户都无感知；
唯一的例外是工具输出瘦身成功但摘要失败——此时保留瘦身结果。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agent_core.compaction.pruner import (
    PruneConfig,
    prune_tool_results,
)
from agent_core.compaction.region import (
    balanced_cut,
    build_checkpoint,
    estimate_tokens,
    extract_previous_summary,
    rebuild,
    select_cut,
)
from agent_core.compaction.spill import SpillStore
from agent_core.compaction.summarizer import summarize
from agent_core.compaction.types import (
    SUMMARY_SOURCE,
    CompactionConfig,
    CompactionError,
    CompactionResult,
)
from agent_core.protocol.messages import Message
from agent_core.middleware import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)
from agent_core.model import ContextUsage, ModelAdapter
from agent_core.compaction.prompts import (
    DEFAULT_COMPACTION_INSTRUCTION,
    DEFAULT_SUMMARY_SECTIONS,
)

logger = logging.getLogger(__name__)

# 计数能力：接收 (messages, tools=...) 并返回真实上下文占用
CountTokens = Callable[..., Awaitable[ContextUsage]]
# 可观测事件上报钩子（独立使用中间件时的兜底；正常情况下走 ctx.emit）
EmitHook = Callable[[str, dict[str, Any]], None]

# ctx.metadata 中记录"上下文超长重试"次数的键
OVERFLOW_RETRY_KEY = "compaction_overflow_retries"

# 各家模型"输入超长"报错的共同特征（OpenAI / Anthropic / Qwen / Gemini 措辞不一）
_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "too many tokens",
    "input length exceeds",
    "reduce the length of the messages",
    "maximum number of tokens",
)


def is_context_overflow_error(exc: BaseException) -> bool:
    """判断异常是否属于"上下文超长"。"""
    text = str(exc).lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


class CompactionMiddleware(Middleware):
    """把窗口压力下的头部旧消息压成结构化摘要。

    依赖由构造注入（中间件的 before_model 拿不到 LLM）：
        llm            : 计数来源，默认取其 context_usage；
        summarizer     : 摘要模型，默认复用主模型（可换成更便宜的小模型）；
        tool_definitions: 计数时需要带上工具 schema，才能算出真实占用。

    用法:
        agent = ReActAgent(
            middleware=[
                CompactionMiddleware(llm, tool_definitions=tool_definitions),
                TokenLimitMiddleware(...),   # 兜底：压缩失败时硬截断
            ],
        )
    """

    def __init__(
        self,
        llm: ModelAdapter,
        *,
        config: CompactionConfig | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
        count_tokens: CountTokens | None = None,
        summarizer: ModelAdapter | None = None,
        prune_config: PruneConfig | None = None,
        spill_store: SpillStore | None = None,
        emit: EmitHook | None = None,
        summary_instruction: str = DEFAULT_COMPACTION_INSTRUCTION,
        summary_sections: tuple[str, ...] = DEFAULT_SUMMARY_SECTIONS,
    ) -> None:
        self._config = config or CompactionConfig()
        self._tool_definitions = tool_definitions
        self._count_tokens: CountTokens = count_tokens or llm.context_usage
        self._summarizer = summarizer or llm
        self._prune_config = prune_config or PruneConfig()
        self._spill_store = spill_store
        self._emit_hook = emit
        self._summary_instruction = summary_instruction
        self._summary_sections = summary_sections
        # 最近一次量到的窗口大小：溢出恢复路径拿不到 usage 时用它兜底
        self._last_window: int | None = None

    # ──────────────────────────────────────────────
    # 入口
    # ──────────────────────────────────────────────

    async def before_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        config = self._config
        if not config.enabled or not ctx.messages:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        usage = await self._safe_measure(ctx.messages)
        if usage is None or not usage.input_tokens or not usage.context_window_tokens:
            # 没有真实占用/窗口信息就无法判断压力，交给兜底中间件
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        window = usage.context_window_tokens
        self._last_window = window
        before_tokens = usage.input_tokens
        ratio = before_tokens / window
        if ratio < config.threshold_ratio:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        self._emit(
            ctx,
            "compaction_started",
            before_tokens=before_tokens,
            window=window,
            ratio=round(ratio, 4),
        )

        try:
            result = await self._compact(
                ctx.messages, window=window, before_tokens=before_tokens
            )
        except Exception as exc:
            logger.warning("[Compaction] 压缩失败，降级到硬截断：%s", exc)
            self._emit(ctx, "compaction_failed", reason=str(exc))
            self._emit(ctx, "compaction_fallback", strategy="trim")
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        if not result.compacted:
            logger.info("[Compaction] 本次未压缩：%s", result.reason)
            self._emit(ctx, "compaction_failed", reason=result.reason)
            self._emit(ctx, "compaction_fallback", strategy="trim")
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        logger.info(
            "[Compaction] 压缩完成：%d → %d 条消息，压入摘要 %d 条，"
            "窗口占用 %d → %d tokens（%.0f%%），reason=%s",
            len(ctx.messages),
            len(result.messages),
            result.cut,
            result.before_tokens,
            result.after_tokens,
            result.after_tokens / window * 100,
            result.reason,
        )
        self._emit(
            ctx,
            "compaction_succeeded",
            before_tokens=result.before_tokens,
            after_tokens=result.after_tokens,
            cut=result.cut,
            ratio=round(result.after_tokens / window, 4),
        )

        ctx.messages = result.messages
        return MiddlewareResult(
            action=MiddlewareAction.MODIFY,
            data={"messages": result.messages, "compaction": result},
        )

    # ──────────────────────────────────────────────
    # 异常接管：模型报"上下文超长"时强制压缩后重试
    # ──────────────────────────────────────────────

    async def handle_exception(self, exc: Exception, ctx: MiddlewareContext) -> bool:
        """接住模型侧的"上下文超长"，强制压一轮并让循环重试当前轮。

        before_model 的阈值判断依赖计数；计数可能低估（估算口径、工具
        schema 变化、多模态内容），所以必须能兜住模型侧的真实报错。

        Returns:
            True 表示已改写 ctx.messages，调用方应重试本次模型调用；
            False 表示不接管，异常按原路径抛出。
        """
        config = self._config
        if not config.enabled or not ctx.messages:
            return False
        if not is_context_overflow_error(exc):
            return False

        attempts = int(ctx.metadata.get(OVERFLOW_RETRY_KEY, 0))
        if attempts >= config.max_overflow_retries:
            logger.warning(
                "[Compaction] 上下文超长重试已达上限（%d），不再强制压缩", attempts
            )
            return False
        ctx.metadata[OVERFLOW_RETRY_KEY] = attempts + 1

        usage = await self._safe_measure(ctx.messages)
        window = None
        before_tokens = 0
        if usage is not None:
            window = usage.context_window_tokens
            before_tokens = usage.input_tokens or 0
        window = window or self._last_window
        if not window:
            logger.warning("[Compaction] 无窗口信息，无法处理上下文超长错误")
            return False
        if not before_tokens:
            before_tokens = estimate_tokens(
                ctx.messages, chars_per_token=config.chars_per_token
            )

        self._emit(
            ctx,
            "compaction_started",
            before_tokens=before_tokens,
            window=window,
            ratio=round(before_tokens / window, 4),
            forced=True,
        )
        try:
            result = await self._compact(
                ctx.messages, window=window, before_tokens=before_tokens
            )
        except Exception as compact_exc:
            logger.warning("[Compaction] 溢出恢复压缩失败：%s", compact_exc)
            self._emit(ctx, "compaction_failed", reason=str(compact_exc))
            return False

        if not result.compacted:
            logger.warning("[Compaction] 溢出恢复未能压缩：%s", result.reason)
            self._emit(ctx, "compaction_failed", reason=result.reason)
            return False

        ctx.messages = result.messages
        logger.info(
            "[Compaction] 溢出恢复压缩完成：%d → %d 条消息，占用 %d → %d tokens",
            len(result.messages) + result.cut - 1,
            len(result.messages),
            result.before_tokens,
            result.after_tokens,
        )
        self._emit(
            ctx,
            "compaction_succeeded",
            before_tokens=result.before_tokens,
            after_tokens=result.after_tokens,
            cut=result.cut,
            ratio=round(result.after_tokens / window, 4),
            forced=True,
        )
        return True

    # ──────────────────────────────────────────────
    # 压缩主流程
    # ──────────────────────────────────────────────

    async def _compact(
        self,
        messages: list[Message],
        *,
        window: int,
        before_tokens: int,
    ) -> CompactionResult:
        """压一轮（必要时按 max_retries 再压一轮），返回压缩结果。

        顺序是"先瘦身超长工具输出，再压摘要"：瘦身粒度细、零成本，
        摘要粒度粗、要调模型，先做便宜的能少调一次模型。

        Raises:
            CompactionError: 摘要失败 / 结构不合规，由调用方降级处理。
        """
        config = self._config
        threshold_tokens = int(window * config.threshold_ratio)
        retain_tokens = int(window * config.retain_ratio)

        # 1. 瘦身：只截断 role == "tool" 的超长输出，不改语义
        pruned, pruned_count = prune_tool_results(
            messages, config=self._prune_config, spill_store=self._spill_store
        )
        pruned_saved = 0
        if pruned_count:
            pruned_saved = max(
                estimate_tokens(messages, chars_per_token=config.chars_per_token)
                - estimate_tokens(pruned, chars_per_token=config.chars_per_token),
                0,
            )
            logger.info(
                "[Compaction] 工具输出瘦身：%d 条，估算省下 %d tokens",
                pruned_count,
                pruned_saved,
            )

        current = pruned
        cut = 0
        after_tokens = max(before_tokens - pruned_saved, 0)

        async def fallback(reason: str) -> CompactionResult:
            """摘要不可用时收口：瘦身有效则单独采纳，否则判定未压缩。"""
            if pruned_saved <= 0:
                return CompactionResult(
                    False, messages, before_tokens=before_tokens, reason=reason
                )
            measured = await self._remaining_tokens(pruned)
            return CompactionResult(
                True,
                pruned,
                before_tokens=before_tokens,
                after_tokens=measured if measured is not None else after_tokens,
                reason=f"prune_only:{reason}",
            )

        # 2. 摘要：压头部旧消息，必要时重复到低于阈值
        for _ in range(config.max_retries + 1):
            # 先按尾部预算取初步切点，再前移到最近的"工具配对平衡边界"，
            # 保证被压头部不遗留无结果的 tool_call、尾部不以孤儿 tool 开头
            cut = balanced_cut(
                current,
                select_cut(
                    current,
                    retain_tokens=retain_tokens,
                    chars_per_token=config.chars_per_token,
                ),
            )
            head_messages = current[:cut]
            head_tokens = estimate_tokens(head_messages, chars_per_token=config.chars_per_token)
            if cut <= 1 or head_tokens <= 0:
                return await fallback("没有可压缩的头部区域")

            # 旧摘要（上一轮写回的 checkpoint）是压缩产物，不算"待压原文"：
            # 它只作为滚动合并的基线，也不参与"新摘要是否更小"的比较。
            previous_summary = extract_previous_summary(current, cut)
            origin_messages = [
                m
                for m in head_messages
                if m.role != "system" and m.name != SUMMARY_SOURCE
            ]
            origin_tokens = estimate_tokens(
                origin_messages, chars_per_token=config.chars_per_token
            )
            if origin_tokens <= 0:
                reason = (
                    "头部只剩已有摘要，重压无收益"
                    if previous_summary is not None
                    else "头部没有可压原文"
                )
                return await fallback(reason)
            if previous_summary is not None:
                logger.info(
                    "[Compaction] 检测到旧摘要（%d 字符），本次按滚动合并处理",
                    len(previous_summary),
                )

            try:
                summary = await summarize(
                    self._summarizer,
                    current,
                    cut=cut,
                    max_tokens=config.summary_max_tokens,
                    instruction=self._summary_instruction,
                    sections=self._summary_sections,
                )
            except CompactionError as exc:
                logger.warning("[Compaction] 摘要失败：%s", exc)
                return await fallback(f"摘要失败:{exc}")

            checkpoint = build_checkpoint(summary)
            checkpoint_tokens = estimate_tokens(
                [checkpoint], chars_per_token=config.chars_per_token
            )
            if checkpoint_tokens >= origin_tokens:
                return await fallback("摘要未比原文更小")

            current = rebuild(current, cut, checkpoint)
            after_tokens = max(
                before_tokens - pruned_saved - (head_tokens - checkpoint_tokens), 0
            )

            remaining = await self._remaining_tokens(current)
            if remaining is not None:
                after_tokens = remaining
            if after_tokens < threshold_tokens:
                break

        reason = "ok" if pruned_count == 0 else f"ok(pruned {pruned_count} tool results)"
        return CompactionResult(
            True,
            current,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            cut=cut,
            reason=reason,
        )

    async def _remaining_tokens(self, messages: list[Message]) -> int | None:
        """压缩后再量一次真实占用；量不到就返回 None，由估算值兜底。"""
        usage = await self._safe_measure(messages)
        return usage.input_tokens if usage is not None else None

    async def _safe_measure(self, messages: list[Message]) -> ContextUsage | None:
        """计数失败不应影响主流程，统一吞掉异常转为 None。"""
        try:
            return await self._count_tokens(messages, tools=self._tool_definitions)
        except Exception as exc:
            logger.warning("[Compaction] token 计数失败：%s", exc)
            return None

    def _emit(self, ctx: MiddlewareContext, event_type: str, **payload: Any) -> None:
        """上报可观测事件；上报本身出问题也不能影响压缩结果。

        优先走 ctx.emit（与 model_retry / model_finished 落在同一事件序列），
        没有注入时退回构造参数 emit（独立使用 / 单测场景）。
        """
        try:
            if ctx.emit is not None:
                ctx.emit(event_type, **payload)
            elif self._emit_hook is not None:
                self._emit_hook(event_type, payload)
        except Exception as exc:
            logger.debug("[Compaction] 事件上报失败：%s", exc)
