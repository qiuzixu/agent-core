"""compaction 子系统的核心单测（此前零测试覆盖）。

覆盖四块：
1. region：尾部保留预算切点 + 工具配对平衡边界（防止孤儿 tool 消息 400）；
2. pruner：超长工具输出瘦身与 spill 回读标记；
3. summarizer：结构校验失败抛 CompactionError，缺小节自动补 (none)；
4. CompactionMiddleware：触发阈值、摘要失败后 prune-only 降级、计数不可用放行。

全部使用假 ModelAdapter / 假计数函数与内存 SpillStore，不触碰 tempfile 与 sqlite。
"""

from __future__ import annotations

import re
import unittest
from collections.abc import AsyncIterator
from typing import Any

from agent_core.compaction.middleware import (
    OVERFLOW_RETRY_KEY,
    CompactionMiddleware,
    is_context_overflow_error,
)
from agent_core.compaction.pruner import (
    PRUNE_MARKER,
    PRUNE_MARKER_SPILL,
    PruneConfig,
    prune_text,
    prune_tool_results,
)
from agent_core.compaction.region import (
    CHECKPOINT_PREAMBLE,
    balanced_cut,
    build_checkpoint,
    estimate_input_tokens,
    estimate_tokens,
    extract_previous_summary,
    rebuild,
    select_cut,
)
from agent_core.compaction.spill import SpillStore
from agent_core.compaction.summarizer import build_summarization_messages, normalize_summary, summarize
from agent_core.compaction.types import (
    SUMMARY_CLOSE,
    SUMMARY_OPEN,
    SUMMARY_SOURCE,
    CompactionConfig,
    CompactionError,
)
from agent_core.middleware import MiddlewareAction, MiddlewareContext
from agent_core.model import ContextUsage, StreamChunk
from agent_core.protocol.messages import (
    Message,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)

WINDOW_TOKENS = 1000


def _big_text(chars: int, filler: str = "x") -> str:
    return filler * chars


class TestRegionCutting(unittest.TestCase):
    """region 配对边界切割：切点必须落在工具配对平衡处。"""

    def test_select_cut_keeps_tail_budget(self) -> None:
        messages = [user_message(_big_text(100, "a")) for _ in range(10)]
        # 每条 25 tokens；保留 50 tokens → 尾部保留 2 条，切点在 index 8。
        self.assertEqual(select_cut(messages, retain_tokens=50), 8)
        # 整段历史都在预算内 → 没有可压缩头部。
        self.assertEqual(select_cut(messages, retain_tokens=1000), 0)

    def test_balanced_cut_moves_before_unpaired_tool_call(self) -> None:
        call = {"id": "call-1", "type": "function", "name": "lookup", "args": {}}
        messages = [
            system_message("sys"),
            user_message("问题"),
            assistant_message("", tool_calls=[call]),
            tool_message(content="结果", tool_call_id="call-1", name="lookup"),
            assistant_message("", tool_calls=[call]),  # 切点落在它之后会遗留无结果的 tool_call
            tool_message(content="结果", tool_call_id="call-1", name="lookup"),
        ]
        # 初步切点 5 落在 assistant 的 tool_call 与它的 tool 结果之间。
        cut = balanced_cut(messages, 5)
        self.assertEqual(cut, 4)
        # 平衡边界只会前移，不会超过初步切点。
        self.assertLessEqual(cut, 5)

    def test_balanced_cut_keeps_already_balanced_point(self) -> None:
        messages = [user_message(_big_text(40)) for _ in range(5)]
        self.assertEqual(balanced_cut(messages, 3), 3)
        self.assertEqual(balanced_cut(messages, 99), 5)

    def test_balanced_cut_never_leaves_orphan_tool_call(self) -> None:
        calls = [
            {"id": "call-1", "type": "function", "name": "a", "args": {}},
            {"id": "call-2", "type": "function", "name": "b", "args": {}},
        ]
        messages = [
            user_message("问题"),
            assistant_message("", tool_calls=calls),  # 一轮两个工具调用
            tool_message(content="r1", tool_call_id="call-1", name="a"),
            tool_message(content="r2", tool_call_id="call-2", name="b"),
        ]
        # 切点 3 会把 call-2 的孤儿结果留在尾部。
        cut = balanced_cut(messages, 3)
        # 头部不遗留无结果的 tool_call；尾部不以孤儿 tool 消息开头。
        self.assertFalse(any(m.tool_calls for m in messages[:cut]))
        self.assertNotEqual(messages[cut].role, "tool")

    def test_extract_previous_summary_and_rebuild(self) -> None:
        messages = [
            system_message("sys"),
            build_checkpoint("旧摘要正文"),
            user_message("后续问题"),
        ]
        self.assertEqual(extract_previous_summary(messages, 2), "旧摘要正文")
        self.assertIsNone(extract_previous_summary([user_message("无标签")], 1))
        rebuilt = rebuild(messages, 2, build_checkpoint("新摘要"))
        self.assertEqual([m.role for m in rebuilt], ["system", "user", "user"])
        self.assertIn(SUMMARY_OPEN, rebuilt[1].content)
        self.assertIn(SUMMARY_CLOSE, rebuilt[1].content)
        self.assertEqual(rebuilt[1].name, SUMMARY_SOURCE)
        self.assertTrue(rebuilt[1].content.startswith(CHECKPOINT_PREAMBLE))

    def test_estimate_tokens_counts_tool_call_payload(self) -> None:
        call = {"id": "call-1", "type": "function", "name": "lookup", "args": {"query": "x"}}
        message = assistant_message("", tool_calls=[call])
        self.assertGreater(estimate_tokens([message]), 1)

    def test_estimate_input_tokens_counts_tool_schema(self) -> None:
        """工具 schema 会随请求发送，估算必须把它计入。"""
        messages = [system_message("sys"), user_message(_big_text(400))]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "查询低空场景",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ]
        without_tools = estimate_input_tokens(messages)
        with_tools = estimate_input_tokens(messages, tool_definitions=tools)
        self.assertGreater(with_tools, without_tools)


class TestPruner(unittest.TestCase):
    """工具输出瘦身与 spill 标记。"""

    def setUp(self) -> None:
        self.config = PruneConfig(threshold_chars=100, head_chars=20, tail_chars=10)

    def test_prune_text_below_threshold_is_untouched(self) -> None:
        self.assertEqual(prune_text("short", self.config), "short")

    def test_prune_text_truncates_with_marker(self) -> None:
        text = "H" * 20 + "M" * 200 + "T" * 10
        pruned = prune_text(text, self.config)
        self.assertTrue(pruned.startswith("H" * 20))
        self.assertTrue(pruned.endswith("T" * 10))
        self.assertIn(PRUNE_MARKER.format(dropped=200).strip(), pruned)
        self.assertLess(len(pruned), len(text))

    def test_prune_tool_results_only_touches_tool_messages(self) -> None:
        user = user_message(_big_text(500))
        long_tool = tool_message(content=_big_text(500), tool_call_id="call-1", name="lookup")
        assistant = assistant_message("回答")
        pruned, count = prune_tool_results([user, long_tool, assistant], config=self.config)
        # 只有 role == "tool" 的超长消息被替换，其余保持原对象。
        self.assertEqual(count, 1)
        self.assertIs(pruned[0], user)
        self.assertIs(pruned[2], assistant)
        self.assertLess(len(pruned[1].content), 500)
        self.assertIn("省略", pruned[1].content)

    def test_prune_tool_results_returns_same_list_when_nothing_pruned(self) -> None:
        short_tool = tool_message(content="短结果", tool_call_id="call-1", name="lookup")
        messages = [user_message("问题"), short_tool]
        pruned, count = prune_tool_results(messages, config=self.config)
        self.assertEqual(count, 0)
        self.assertIs(pruned, messages)

    def test_prune_with_spill_store_marks_readback_id(self) -> None:
        store = SpillStore()
        text = "H" * 20 + "M" * 200 + "T" * 10
        pruned = prune_text(text, self.config, spill_store=store)
        self.assertIn("read_spilled_content", pruned)
        self.assertIn("spill-", pruned)
        # 标记中的 spill_id 可以回读完整原文。
        match = re.search(r"spill-[0-9a-f]+", pruned)
        assert match is not None
        spill_id = match.group(0)
        self.assertEqual(store.get(spill_id), text)
        self.assertIn(PRUNE_MARKER_SPILL.format(dropped=200, spill_id=spill_id).strip(), pruned)

    def test_prune_falls_back_when_spill_rejects_oversized_entry(self) -> None:
        store = SpillStore(max_chars_per_entry=10)
        text = "H" * 20 + "M" * 200 + "T" * 10
        pruned = prune_text(text, self.config, spill_store=store)
        # 超限条目不存入，标记退化为纯截断标记。
        self.assertNotIn("spill-", pruned)
        self.assertIn("省略 200 字符", pruned)
        self.assertEqual(len(store), 0)

    def test_prune_config_rejects_invalid_budget(self) -> None:
        with self.assertRaises(ValueError):
            PruneConfig(threshold_chars=100, head_chars=60, tail_chars=60)


class TestSummarizer(unittest.IsolatedAsyncioTestCase):
    """摘要调用与结构校验。"""

    async def test_normalize_summary_fills_missing_sections(self) -> None:
        summary = normalize_summary("## 任务目标\n查询机场")
        self.assertIn("## 任务目标", summary)
        self.assertIn("## 当前状态\n(none)", summary)

    async def test_normalize_summary_rejects_unstructured_output(self) -> None:
        self.assertEqual(normalize_summary("没有任何小节的自由文本"), "")
        self.assertEqual(normalize_summary("   "), "")

    async def test_summarize_raises_compaction_error_on_bad_structure(self) -> None:
        model = _FakeModel(reply="模型没有按格式输出")

        with self.assertRaises(CompactionError):
            await summarize(model, [user_message("历史")], cut=1, max_tokens=256)

    async def test_summarize_wraps_model_failure(self) -> None:
        model = _FakeModel(stream_error=RuntimeError("网络中断"))

        with self.assertRaises(CompactionError):
            await summarize(model, [user_message("历史")], cut=1, max_tokens=256)

    async def test_summarize_accepts_structured_output(self) -> None:
        model = _FakeModel(reply="## 任务目标\n压缩历史")
        summary = await summarize(model, [system_message("sys"), user_message("历史")], cut=2, max_tokens=256)
        self.assertIn("## 任务目标", summary)
        # 摘要输入 = 原 system + 被压头部 + 指令，system 仍在首位。
        prompt = build_summarization_messages([system_message("sys"), user_message("历史")], cut=2)
        self.assertEqual(prompt[0].role, "system")
        self.assertIn("压缩", prompt[-1].content)


class _FakeModel:
    """最小 ModelAdapter 替身：可注入摘要回复、异常或流式行为。"""

    def __init__(
        self,
        *,
        reply: str | None = None,
        error: Exception | None = None,
        finish_reason: str | None = "stop",
        stream_tool_calls: bool = False,
        stream_error: Exception | None = None,
        error_when_prompt_chars_above: int | None = None,
    ) -> None:
        self.reply = reply
        self.error = error
        self.finish_reason = finish_reason
        self.stream_tool_calls = stream_tool_calls
        self.stream_error = stream_error
        self.error_when_prompt_chars_above = error_when_prompt_chars_above
        self.summary_calls = 0
        self.prompts: list[list[Message]] = []

    async def chat(self, messages: list[Message], **kwargs: Any) -> Message:
        del kwargs
        self.summary_calls += 1
        if self.error is not None:
            raise self.error
        return assistant_message(self.reply or "")

    async def stream_chat(
        self, messages: list[Message], **kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        self.prompts.append(list(messages))
        self.summary_calls += 1
        del kwargs
        if self.stream_error is not None:
            raise self.stream_error
        if self.error is not None:
            raise self.error
        reply = self.reply or ""
        # 分片输出，模拟真实流式
        for i in range(0, len(reply), 50):
            yield StreamChunk(text=reply[i : i + 50])
        if self.stream_tool_calls:
            yield StreamChunk(tool_calls=[{"id": "c1", "type": "function", "name": "x", "args": {}}])
        yield StreamChunk(finish_reason=self.finish_reason)

    async def context_usage(self, messages: list[Message], **kwargs: Any) -> ContextUsage:
        del kwargs
        return ContextUsage(
            input_tokens=estimate_tokens(messages),
            context_window_tokens=WINDOW_TOKENS,
            exact=True,
            source="test",
        )


def _long_history_with_tool_output() -> list[Message]:
    """构造会触发压缩的历史：一条超长工具输出 + 一批尾部用户消息。"""
    call = {"id": "call-1", "type": "function", "name": "lookup", "args": {"query": "机场"}}
    messages: list[Message] = [
        system_message("sys"),
        user_message(_big_text(100)),
        assistant_message("", tool_calls=[call]),
        tool_message(content=_big_text(2000, "D"), tool_call_id="call-1", name="lookup"),
    ]
    messages.extend(user_message(_big_text(100, f"u{i}")) for i in range(20))
    return messages


class TestCompactionMiddleware(unittest.IsolatedAsyncioTestCase):
    """CompactionMiddleware：阈值触发与降级路径。"""

    def _middleware(
        self,
        model: _FakeModel,
        *,
        config: CompactionConfig | None = None,
        prune_config: PruneConfig | None = None,
        spill_store: SpillStore | None = None,
        events: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> CompactionMiddleware:
        captured = events if events is not None else []

        def emit(event_type: str, payload: dict[str, Any]) -> None:
            captured.append((event_type, payload))

        return CompactionMiddleware(
            model,
            config=config or CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            prune_config=prune_config or PruneConfig(threshold_chars=100, head_chars=20, tail_chars=10),
            spill_store=spill_store,
            emit=emit,
        )

    async def test_below_threshold_continues_without_compaction(self) -> None:
        model = _FakeModel(reply="## 任务目标\n不应被调用")
        events: list[tuple[str, dict[str, Any]]] = []
        middleware = self._middleware(model, events=events)
        messages = [system_message("sys"), user_message(_big_text(100))]  # 27 tokens << 500
        ctx = MiddlewareContext(messages=messages)

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.CONTINUE)
        self.assertEqual(model.summary_calls, 0)
        self.assertEqual(events, [])

    async def test_unavailable_usage_continues(self) -> None:
        model = _FakeModel(reply="## 任务目标\n压缩历史")

        async def unavailable(messages: list[Message], tools: Any = None) -> ContextUsage:
            del messages, tools
            return ContextUsage(
                input_tokens=None,
                context_window_tokens=None,
                exact=False,
                source="unavailable",
            )

        middleware = CompactionMiddleware(
            model,
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            count_tokens=unavailable,
        )
        ctx = MiddlewareContext(messages=_long_history_with_tool_output())

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.CONTINUE)
        self.assertEqual(model.summary_calls, 0)

    async def test_disabled_config_continues(self) -> None:
        model = _FakeModel(reply="## 任务目标\n压缩历史")
        middleware = self._middleware(
            model,
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2, enabled=False),
        )
        ctx = MiddlewareContext(messages=_long_history_with_tool_output())

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.CONTINUE)
        self.assertEqual(model.summary_calls, 0)

    async def test_threshold_reached_compacts_head_into_checkpoint(self) -> None:
        model = _FakeModel(reply="## 任务目标\n查询机场并返回结果")
        events: list[tuple[str, dict[str, Any]]] = []
        middleware = self._middleware(model, events=events)
        messages = _long_history_with_tool_output()
        ctx = MiddlewareContext(messages=messages)

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.MODIFY)
        compacted = result.data["messages"]
        # system 保留在首位，头部原文被替换成 checkpoint 消息。
        self.assertEqual(compacted[0].role, "system")
        self.assertEqual(compacted[1].name, SUMMARY_SOURCE)
        self.assertIn(SUMMARY_OPEN, compacted[1].content)
        # 尾部原文保留，孤儿 tool 结果不会出现在压缩边界之后。
        self.assertFalse(compacted[1].role == "tool")
        # 压缩后占用低于阈值。
        compaction = result.data["compaction"]
        self.assertTrue(compaction.compacted)
        self.assertLess(compaction.after_tokens, WINDOW_TOKENS * 0.5)
        event_types = [event for event, _payload in events]
        self.assertIn("compaction_started", event_types)
        self.assertIn("compaction_succeeded", event_types)

    async def test_summary_failure_falls_back_to_prune_only(self) -> None:
        # 摘要模型输出不符合结构 → CompactionError → 只采纳瘦身结果。
        model = _FakeModel(reply="自由发挥，没有任何小节")
        events: list[tuple[str, dict[str, Any]]] = []
        spill_store = SpillStore()
        middleware = self._middleware(model, events=events, spill_store=spill_store)
        messages = _long_history_with_tool_output()
        ctx = MiddlewareContext(messages=messages)

        result = await middleware.before_model(ctx)

        # 瘦身省下的空间足够大 → prune-only 单独成立。
        self.assertEqual(result.action, MiddlewareAction.MODIFY)
        compaction = result.data["compaction"]
        self.assertTrue(compaction.compacted)
        self.assertTrue(compaction.reason.startswith("prune_only:"))
        # 没有写入摘要 checkpoint；工具输出带瘦身标记，可经 spill 回读。
        self.assertFalse(any(m.name == SUMMARY_SOURCE for m in compaction.messages))
        pruned_tool = next(m for m in compaction.messages if m.role == "tool")
        self.assertIn("read_spilled_content", pruned_tool.content)
        self.assertGreater(len(spill_store), 0)
        # prune-only 被单独采纳时按成功收口（原因记录在 result.reason）。
        event_types = [event for event, _payload in events]
        self.assertEqual(event_types, ["compaction_started", "compaction_succeeded"])

    async def test_summary_failure_without_prunable_content_continues(self) -> None:
        # 没有超长工具输出、摘要又失败 → 本次未压缩，交给兜底硬截断。
        model = _FakeModel(reply="自由发挥，没有任何小节")
        events: list[tuple[str, dict[str, Any]]] = []
        middleware = self._middleware(model, events=events)
        messages = [system_message("sys"), *(user_message(_big_text(100, f"u{i}")) for i in range(30))]
        ctx = MiddlewareContext(messages=messages)

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.CONTINUE)
        event_types = [event for event, _payload in events]
        self.assertIn("compaction_failed", event_types)
        self.assertIn("compaction_fallback", event_types)

    async def test_summary_not_smaller_than_head_falls_back(self) -> None:
        # 摘要比被压原文还大 → 判定无收益，走 prune-only / CONTINUE。
        model = _FakeModel(reply="## 任务目标\n" + _big_text(4000, "长"))
        middleware = self._middleware(model)
        messages = [system_message("sys"), *(user_message(_big_text(100, f"u{i}")) for i in range(30))]
        ctx = MiddlewareContext(messages=messages)

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.CONTINUE)

    async def test_overflow_error_triggers_forced_compaction(self) -> None:
        model = _FakeModel(reply="## 任务目标\n压缩历史")
        events: list[tuple[str, dict[str, Any]]] = []
        middleware = self._middleware(model, events=events)
        messages = _long_history_with_tool_output()
        ctx = MiddlewareContext(messages=messages)

        self.assertTrue(is_context_overflow_error(Exception("prompt is too long: 9999 tokens")))
        self.assertFalse(is_context_overflow_error(RuntimeError("其他错误")))
        # 非溢出错误不接管。
        self.assertFalse(await middleware.handle_exception(RuntimeError("普通失败"), ctx))
        # 溢出错误：强制压缩一轮并要求重试。
        self.assertTrue(await middleware.handle_exception(Exception("prompt is too long"), ctx))
        self.assertEqual(ctx.metadata[OVERFLOW_RETRY_KEY], 1)
        self.assertEqual(ctx.messages[1].name, SUMMARY_SOURCE)
        event_types = [event for event, _payload in events]
        self.assertIn("compaction_succeeded", event_types)
        # 重试次数达到上限后不再接管。
        self.assertFalse(await middleware.handle_exception(Exception("prompt is too long"), ctx))
        self.assertFalse(await middleware.handle_exception(Exception("prompt is too long"), ctx))

    # ──────────────────────────────────────────────
    # 计数兜底与输出预留
    # ──────────────────────────────────────────────

    async def test_missing_input_tokens_falls_back_to_estimate(self) -> None:
        """Provider 没有 tokenizer（input_tokens=None）时按字符估算，主动压缩仍能触发。"""
        model = _FakeModel(reply="## 任务目标\n压缩历史")

        async def no_tokenizer(messages: list[Message], tools: Any = None) -> ContextUsage:
            del messages, tools
            return ContextUsage(
                input_tokens=None,
                context_window_tokens=WINDOW_TOKENS,
                exact=False,
                source="provider_without_tokenizer",
            )

        middleware = CompactionMiddleware(
            model,
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            count_tokens=no_tokenizer,
        )
        ctx = MiddlewareContext(messages=_long_history_with_tool_output())

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.MODIFY)
        self.assertTrue(result.data["compaction"].compacted)

    async def test_estimate_when_unavailable_can_be_disabled(self) -> None:
        """关闭估算兜底后，精确计数不可用就放行（交给硬截断兜底）。"""
        model = _FakeModel(reply="## 任务目标\n压缩历史")

        async def no_tokenizer(messages: list[Message], tools: Any = None) -> ContextUsage:
            del messages, tools
            return ContextUsage(
                input_tokens=None,
                context_window_tokens=WINDOW_TOKENS,
                exact=False,
                source="provider_without_tokenizer",
            )

        middleware = CompactionMiddleware(
            model,
            config=CompactionConfig(
                threshold_ratio=0.5,
                retain_ratio=0.2,
                estimate_when_unavailable=False,
            ),
            count_tokens=no_tokenizer,
        )
        ctx = MiddlewareContext(messages=_long_history_with_tool_output())

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.CONTINUE)
        self.assertEqual(model.summary_calls, 0)

    async def test_configured_window_enables_estimation(self) -> None:
        """Provider 既不给计数也不给窗口时，显式配置窗口仍可启用估算兜底。"""
        model = _FakeModel(reply="## 任务目标\n压缩历史")

        async def nothing(messages: list[Message], tools: Any = None) -> ContextUsage:
            del messages, tools
            return ContextUsage(input_tokens=None, context_window_tokens=None, exact=False, source="unavailable")

        middleware = CompactionMiddleware(
            model,
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            count_tokens=nothing,
            context_window_tokens=WINDOW_TOKENS,
        )
        ctx = MiddlewareContext(messages=_long_history_with_tool_output())

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.MODIFY)

    async def test_output_reserve_lowers_effective_window(self) -> None:
        """输出预留会压缩可用输入窗口，让压缩更早触发。"""
        model = _FakeModel(reply="## 任务目标\n压缩历史")
        # 17 条 100 字符消息 ≈ 401 tokens：低于 1000×0.5=500，但高于 (1000-400)×0.5=300。
        messages = [system_message("sys")]
        messages.extend(user_message(_big_text(100)) for _ in range(16))

        without_reserve = CompactionMiddleware(
            model,
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            reserved_output_tokens=0,
        )
        first = await without_reserve.before_model(MiddlewareContext(messages=list(messages)))
        self.assertEqual(first.action, MiddlewareAction.CONTINUE)

        with_reserve = CompactionMiddleware(
            model,
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            reserved_output_tokens=400,
        )
        second = await with_reserve.before_model(MiddlewareContext(messages=list(messages)))
        self.assertEqual(second.action, MiddlewareAction.MODIFY)


class TestSummaryTruncationGuard(unittest.IsolatedAsyncioTestCase):
    """摘要防截断校验（学 pi：finish_reason 拒绝落库 + 启发式兜底）。"""

    async def test_length_finish_reason_rejected(self) -> None:
        """finish_reason=length 表示输出被截断，拒绝写入残缺摘要。"""
        model = _FakeModel(reply="## 任务目标\n太长的摘要", finish_reason="length")
        with self.assertRaises(CompactionError) as ctx:
            await summarize(model, [user_message("历史")], cut=1, max_tokens=256)
        self.assertIn("截断", str(ctx.exception))

    async def test_max_tokens_finish_reason_rejected(self) -> None:
        """部分端点用 max_tokens 表示截断，同样拒绝。"""
        model = _FakeModel(reply="## 任务目标\n截断", finish_reason="max_tokens")
        with self.assertRaises(CompactionError):
            await summarize(model, [user_message("历史")], cut=1, max_tokens=256)

    async def test_tool_call_stream_rejected(self) -> None:
        """流中出现工具调用说明模型没有按摘要指令输出，拒绝。"""
        model = _FakeModel(reply="## 任务目标\n摘要", stream_tool_calls=True)
        with self.assertRaises(CompactionError) as ctx:
            await summarize(model, [user_message("历史")], cut=1, max_tokens=256)
        self.assertIn("工具调用", str(ctx.exception))

    async def test_no_finish_reason_near_limit_rejected(self) -> None:
        """适配器不上报 finish_reason 时用启发式：估算 ≥ 95% 上限视为疑似截断。"""
        # 300 字符 ASCII 回复 ≈ 75 tokens ≥ 64 × 0.95 = 60
        model = _FakeModel(reply="## 任务目标\n" + "x" * 300, finish_reason=None)
        with self.assertRaises(CompactionError) as ctx:
            await summarize(model, [user_message("历史")], cut=1, max_tokens=64)
        self.assertIn("疑似被截断", str(ctx.exception))

    async def test_normal_summary_passes(self) -> None:
        """正常 stop 结束的结构化摘要不受影响。"""
        model = _FakeModel(reply="## 任务目标\n压缩历史")
        summary = await summarize(model, [user_message("历史")], cut=1, max_tokens=256)
        self.assertIn("## 任务目标", summary)


class TestCompactionBreaker(unittest.IsolatedAsyncioTestCase):
    """摘要熔断（学 Claude Code：连续失败 ≥3 跳过摘要只做瘦身）。"""

    def _long_history_no_tool(self) -> list[Message]:
        """没有工具输出的长历史，瘦身不生效，必须依赖摘要。"""
        return [system_message("sys"), *(user_message(_big_text(100, f"u{i}")) for i in range(30))]

    async def test_breaker_trips_after_consecutive_failures(self) -> None:
        """连续失败 3 次后，第 4 次不再调用摘要模型。"""
        model = _FakeModel(reply="自由发挥，没有小节")
        config = CompactionConfig(
            threshold_ratio=0.5,
            retain_ratio=0.2,
            max_consecutive_failures=3,
            failure_cooldown_seconds=0,
        )
        middleware = CompactionMiddleware(model, config=config)
        # 3 次触发
        for _ in range(3):
            ctx = MiddlewareContext(messages=self._long_history_no_tool())
            result = await middleware.before_model(ctx)
            self.assertEqual(result.action, MiddlewareAction.CONTINUE)
        self.assertEqual(model.summary_calls, 3)
        # 第 4 次：熔断打开，跳过摘要
        ctx = MiddlewareContext(messages=self._long_history_no_tool())
        result = await middleware.before_model(ctx)
        self.assertEqual(result.action, MiddlewareAction.CONTINUE)
        self.assertEqual(model.summary_calls, 3)
        # reason 含"熔断"
        events: list[tuple[str, dict[str, Any]]] = []
        middleware2 = CompactionMiddleware(
            model, config=config,
            emit=lambda t, p: events.append((t, p)),
        )
        # （上面的 middleware2 是新的，不复用熔断状态；直接检查原始 middleware）
        # 熔断事件在 _record_summary_failure 中发出（需要 ctx），无 ctx 时走 emit hook
        middleware3 = CompactionMiddleware(
            model, config=config,
            emit=lambda t, p: events.append((t, p)),
        )
        # 触发 3 次
        for _ in range(3):
            ctx = MiddlewareContext(messages=self._long_history_no_tool())
            await middleware3.before_model(ctx)
        types = [t for t, _ in events]
        self.assertIn("compaction_breaker_tripped", types)

    async def test_breaker_cooldown_half_open_probe(self) -> None:
        """冷却结束后放行一次探测（半开）。"""
        import time as _time
        model = _FakeModel(reply="自由发挥，没有小节")
        config = CompactionConfig(
            threshold_ratio=0.5,
            retain_ratio=0.2,
            max_consecutive_failures=2,
            failure_cooldown_seconds=0.01,
        )
        middleware = CompactionMiddleware(model, config=config)
        for _ in range(2):
            ctx = MiddlewareContext(messages=self._long_history_no_tool())
            await middleware.before_model(ctx)
        self.assertTrue(middleware._summary_breaker_open())
        _time.sleep(0.02)
        self.assertFalse(middleware._summary_breaker_open())

    async def test_breaker_resets_on_success(self) -> None:
        """摘要成功后熔断计数复位。"""
        model = _FakeModel(reply="自由发挥，没有小节")
        config = CompactionConfig(
            threshold_ratio=0.5,
            retain_ratio=0.2,
            max_consecutive_failures=2,
            failure_cooldown_seconds=0,
        )
        middleware = CompactionMiddleware(model, config=config)
        # 失败 1 次
        ctx = MiddlewareContext(messages=self._long_history_no_tool())
        await middleware.before_model(ctx)
        self.assertEqual(middleware._summary_failure_count, 1)
        # 换一个成功模型
        ok_model = _FakeModel(reply="## 任务目标\n压缩历史")
        middleware._summarizer = ok_model
        ctx = MiddlewareContext(messages=self._long_history_no_tool())
        await middleware.before_model(ctx)
        self.assertEqual(middleware._summary_failure_count, 0)
        self.assertIsNone(middleware._summary_breaker_tripped_at)

    async def test_breaker_not_applied_to_forced_path(self) -> None:
        """溢出强制路径（handle_exception）不受熔断限制。"""
        model = _FakeModel(reply="自由发挥，没有小节")
        config = CompactionConfig(
            threshold_ratio=0.5,
            retain_ratio=0.2,
            max_consecutive_failures=1,
            failure_cooldown_seconds=0,
        )
        middleware = CompactionMiddleware(model, config=config)
        # 先失败 1 次触发熔断
        ctx = MiddlewareContext(messages=self._long_history_no_tool())
        await middleware.before_model(ctx)
        self.assertTrue(middleware._summary_breaker_open())
        # 溢出路径仍然会尝试摘要
        ctx2 = MiddlewareContext(messages=self._long_history_no_tool())
        result = await middleware.handle_exception(Exception("prompt is too long: 9999 tokens"), ctx2)
        # 摘要又失败了（reply 没变），所以返回 False
        self.assertFalse(result)
        self.assertEqual(model.summary_calls, 2)


class TestSummaryShrinkRetry(unittest.IsolatedAsyncioTestCase):
    """摘要输入超长时按半数收缩切点重试（学 Codex / Claude Code）。"""

    async def test_shrink_retry_on_overflow(self) -> None:
        """摘要输入超长 → 切点减半重试 → 第二次成功。"""
        call_count = 0

        class ShrinkModel:
            def __init__(self) -> None:
                self.calls = 0
                self.prompts: list[list[Message]] = []

            async def stream_chat(self, messages, **kwargs):
                self.calls += 1
                self.prompts.append(list(messages))
                prompt_chars = sum(len(m.content or "") for m in messages)
                if self.calls == 1 and prompt_chars > 1500:
                    raise RuntimeError("prompt is too long: 5000 tokens > 4000 limit")
                yield StreamChunk(text="## 任务目标\n收缩后摘要")
                yield StreamChunk(finish_reason="stop")

            async def chat(self, messages, **kwargs):
                raise NotImplementedError

            async def context_usage(self, messages, **kwargs):
                del kwargs
                return ContextUsage(
                    input_tokens=estimate_tokens(messages),
                    context_window_tokens=WINDOW_TOKENS,
                    exact=True,
                    source="test",
                )

        model = ShrinkModel()
        config = CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2, max_shrink_retries=1)
        middleware = CompactionMiddleware(model, config=config)
        messages = [system_message("sys"), *(user_message("x" * 100 + str(i)) for i in range(30))]
        ctx = MiddlewareContext(messages=messages)

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.MODIFY)
        self.assertEqual(model.calls, 3)  # 1 超长 + 1 收缩成功 + 1 下一轮压缩
        self.assertTrue(result.data["compaction"].compacted)
        # 第二次的 prompt 应该更短
        first_chars = sum(len(m.content or "") for m in model.prompts[0])
        second_chars = sum(len(m.content or "") for m in model.prompts[1])
        self.assertLess(second_chars, first_chars)

    async def test_shrink_exhausted_falls_back(self) -> None:
        """收缩重试耗尽后放弃，走降级路径。"""
        model = _FakeModel(stream_error=RuntimeError("prompt is too long: 99999 tokens"))
        config = CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2, max_shrink_retries=1)
        middleware = CompactionMiddleware(model, config=config)
        messages = [system_message("sys"), *(user_message(_big_text(100, f"u{i}")) for i in range(30))]
        ctx = MiddlewareContext(messages=messages)

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.CONTINUE)
        # 初始 + 收缩 1 次 = 2 次
        self.assertEqual(model.summary_calls, 2)


class TestAdaptiveSummaryTokens(unittest.IsolatedAsyncioTestCase):
    """summary_max_tokens 自适应（默认 0 → 按窗口 10% 计算）。"""

    async def test_adaptive_returns_2048_for_small_window(self) -> None:
        """窗口 1000 → effective_window ~1000 → max(2048, 100) = 2048。"""
        middleware = CompactionMiddleware(
            _FakeModel(reply="## 任务目标\n摘要"),
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2, summary_max_tokens=0),
            context_window_tokens=WINDOW_TOKENS,
        )
        self.assertEqual(middleware._resolve_summary_max_tokens(1000), 2048)

    async def test_adaptive_capped_at_8192(self) -> None:
        """超大窗口 → min(8192, max(2048, ...)) = 8192。"""
        middleware = CompactionMiddleware(
            _FakeModel(reply="## 任务目标\n摘要"),
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2, summary_max_tokens=0),
            context_window_tokens=200000,
        )
        self.assertEqual(middleware._resolve_summary_max_tokens(200000), 8192)

    async def test_explicit_value_passes_through(self) -> None:
        """显式配置 > 0 时直接使用。"""
        middleware = CompactionMiddleware(
            _FakeModel(reply="## 任务目标\n摘要"),
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2, summary_max_tokens=512),
            context_window_tokens=WINDOW_TOKENS,
        )
        self.assertEqual(middleware._resolve_summary_max_tokens(1000), 512)

    async def test_negative_summary_max_tokens_raises(self) -> None:
        """负数配置直接拒绝。"""
        with self.assertRaises(ValueError):
            CompactionConfig(summary_max_tokens=-1)


class TestCompactionObservability(unittest.IsolatedAsyncioTestCase):
    """埋点：duration_ms / summary_tokens 出现在 compaction_succeeded 事件中。"""

    async def test_succeeded_event_has_timing_and_tokens(self) -> None:
        model = _FakeModel(reply="## 任务目标\n压缩历史")
        events: list[tuple[str, dict[str, Any]]] = []
        middleware = CompactionMiddleware(
            model,
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            emit=lambda t, p: events.append((t, p)),
        )
        messages = _long_history_with_tool_output()
        ctx = MiddlewareContext(messages=messages)

        await middleware.before_model(ctx)

        succeeded = [p for t, p in events if t == "compaction_succeeded"]
        self.assertTrue(succeeded)
        payload = succeeded[0]
        self.assertIn("duration_ms", payload)
        self.assertIsInstance(payload["duration_ms"], float)
        self.assertIn("summary_tokens", payload)
        self.assertIsInstance(payload["summary_tokens"], int)


if __name__ == "__main__":
    unittest.main()
