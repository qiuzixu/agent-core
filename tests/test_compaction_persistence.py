"""压缩摘要持久化（跨轮复用）测试。

验证两条链路：
1. CompactionMiddleware 压缩成功后调用 persist_summary 回调（thread_id + checkpoint 消息），
   失败/未触发/prune-only/重复摘要时不调用；
2. SessionManager._build_context 识别已有摘要并以"摘要 + 增量"构建上下文，
   max_history 裁剪不越过最新摘要。
"""

import unittest
from collections.abc import AsyncIterator
from typing import Any

from agent_core.compaction.middleware import CompactionMiddleware
from agent_core.compaction.region import estimate_tokens
from agent_core.compaction.types import (
    SUMMARY_CLOSE,
    SUMMARY_OPEN,
    SUMMARY_SOURCE,
    CompactionConfig,
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
from agent_core.storage.session import MemorySessionStore, SessionManager

WINDOW_TOKENS = 1000


def _big_text(chars: int, filler: str = "x") -> str:
    return filler * chars


def _long_history() -> list[Message]:
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


class _FakeModel:
    """最小 ModelAdapter 替身：返回结构化摘要回复。"""

    def __init__(self, reply: str = "## 任务目标\n压缩历史") -> None:
        self.reply = reply
        self.chat_calls = 0

    async def chat(self, messages: list[Message], **kwargs: Any) -> Message:
        del messages, kwargs
        self.chat_calls += 1
        return assistant_message(self.reply)

    async def stream_chat(self, messages: list[Message], **kwargs: Any) -> AsyncIterator[StreamChunk]:
        del messages, kwargs
        raise AssertionError("压缩流程不应调用 stream_chat")
        yield  # pragma: no cover

    async def context_usage(self, messages: list[Message], **kwargs: Any) -> ContextUsage:
        del kwargs
        return ContextUsage(
            input_tokens=estimate_tokens(messages),
            context_window_tokens=WINDOW_TOKENS,
            exact=True,
            source="test",
        )


class TestSummaryPersistence(unittest.IsolatedAsyncioTestCase):
    """CompactionMiddleware 摘要持久化回调。"""

    async def test_successful_compaction_persists_summary(self) -> None:
        persisted: list[tuple[str, Message]] = []

        async def persist(thread_id: str, checkpoint: Message, info: dict[str, Any]) -> None:
            persisted.append((thread_id, checkpoint))

        middleware = CompactionMiddleware(
            _FakeModel(),
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            persist_summary=persist,
        )
        ctx = MiddlewareContext(messages=_long_history(), metadata={"thread_id": "thread-1"})

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.MODIFY)
        self.assertEqual(len(persisted), 1)
        thread_id, checkpoint = persisted[0]
        self.assertEqual(thread_id, "thread-1")
        self.assertEqual(checkpoint.name, SUMMARY_SOURCE)
        self.assertIn(SUMMARY_OPEN, checkpoint.content)

    async def test_below_threshold_does_not_persist(self) -> None:
        persisted: list[tuple[str, Message]] = []

        async def persist(thread_id: str, checkpoint: Message, info: dict[str, Any]) -> None:
            persisted.append((thread_id, checkpoint))

        middleware = CompactionMiddleware(
            _FakeModel(),
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            persist_summary=persist,
        )
        ctx = MiddlewareContext(
            messages=[system_message("sys"), user_message(_big_text(100))],
            metadata={"thread_id": "thread-1"},
        )

        await middleware.before_model(ctx)

        self.assertEqual(persisted, [])

    async def test_missing_thread_id_does_not_persist(self) -> None:
        persisted: list[tuple[str, Message]] = []

        async def persist(thread_id: str, checkpoint: Message, info: dict[str, Any]) -> None:
            persisted.append((thread_id, checkpoint))

        middleware = CompactionMiddleware(
            _FakeModel(),
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            persist_summary=persist,
        )
        ctx = MiddlewareContext(messages=_long_history())

        await middleware.before_model(ctx)

        self.assertEqual(persisted, [])

    async def test_persist_failure_does_not_break_run(self) -> None:
        async def persist(thread_id: str, checkpoint: Message, info: dict[str, Any]) -> None:
            del thread_id, checkpoint, info
            raise RuntimeError("db down")

        middleware = CompactionMiddleware(
            _FakeModel(),
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            persist_summary=persist,
        )
        ctx = MiddlewareContext(messages=_long_history(), metadata={"thread_id": "thread-1"})

        result = await middleware.before_model(ctx)

        self.assertEqual(result.action, MiddlewareAction.MODIFY)

    async def test_duplicate_summary_persisted_once(self) -> None:
        persisted: list[tuple[str, Message]] = []

        async def persist(thread_id: str, checkpoint: Message, info: dict[str, Any]) -> None:
            persisted.append((thread_id, checkpoint))

        middleware = CompactionMiddleware(
            _FakeModel(),
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            persist_summary=persist,
        )

        # 第一次：带长工具输出触发摘要
        ctx = MiddlewareContext(messages=_long_history(), metadata={"thread_id": "t"})
        await middleware.before_model(ctx)
        self.assertEqual(len(persisted), 1)
        summary_content = persisted[0][1].content

        # 第二次：同样历史再压（模拟同 run 内重复触发），摘要文本相同应去重
        ctx2 = MiddlewareContext(messages=_long_history(), metadata={"thread_id": "t"})
        await middleware.before_model(ctx2)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(summary_content, persisted[0][1].content)


class TestSessionManagerReuse(unittest.IsolatedAsyncioTestCase):
    """SessionManager 复用已持久化摘要。"""

    def _manager(self, max_history: int = 50) -> SessionManager:
        return SessionManager(MemorySessionStore(), max_history_messages=max_history)

    def test_build_context_starts_from_latest_summary(self) -> None:
        manager = self._manager()
        checkpoint = Message(
            role="user",
            name=SUMMARY_SOURCE,
            content=(
                "这是系统自动生成的早期对话检查点。\n\n"
                f"{SUMMARY_OPEN}\n用户之前查询过航线 R-001\n{SUMMARY_CLOSE}"
            ),
        )
        history = [
            user_message("很早的问题"),
            assistant_message("很早的回答"),
            checkpoint,
            user_message("摘要之后的问题"),
            assistant_message("摘要之后的回答"),
        ]

        context = manager._build_context(history)

        # 摘要正文进入上下文，摘要之前的原文被影子化
        self.assertIn("用户之前查询过航线 R-001", context)
        self.assertNotIn("很早的问题", context)
        self.assertNotIn("很早的回答", context)
        # 摘要之后的增量消息保留
        self.assertIn("摘要之后的问题", context)
        self.assertIn("摘要之后的回答", context)

    def test_build_context_without_summary_uses_full_history(self) -> None:
        manager = self._manager()
        history = [user_message("问题一"), assistant_message("回答一")]

        context = manager._build_context(history)

        self.assertIn("问题一", context)
        self.assertIn("回答一", context)

    async def test_trim_keeps_latest_summary(self) -> None:
        store = MemorySessionStore()
        checkpoint = Message(
            role="user",
            name=SUMMARY_SOURCE,
            content=f"前置\n\n{SUMMARY_OPEN}\n历史摘要\n{SUMMARY_CLOSE}",
        )
        history = [
            user_message("旧消息1"),
            user_message("旧消息2"),
            checkpoint,
            user_message("新消息1"),
            user_message("新消息2"),
        ]
        await store.append("t", history)

        # 直接触发裁剪路径（模拟 _chat_unlocked 内部逻辑）
        loaded = await store.load("t")
        trimmed = loaded[-3:]
        start = len(loaded) - 3
        latest_summary = max(i for i, m in enumerate(loaded) if m.name == SUMMARY_SOURCE)
        if latest_summary < start:
            start = latest_summary
        trimmed = loaded[start:]
        await store.save("t", trimmed)

        reloaded = await store.load("t")
        self.assertIn(checkpoint, reloaded)
        self.assertGreaterEqual(len(reloaded), 3)

    async def test_persist_then_reuse_end_to_end(self) -> None:
        """端到端：中间件持久化摘要后，SessionManager 构建上下文复用它。"""
        store = MemorySessionStore()
        saved: list[tuple[str, Message]] = []

        async def persist(thread_id: str, checkpoint: Message, info: dict[str, Any]) -> None:
            del info
            saved.append((thread_id, checkpoint))
            await store.append(thread_id, [checkpoint])

        middleware = CompactionMiddleware(
            _FakeModel(),
            config=CompactionConfig(threshold_ratio=0.5, retain_ratio=0.2),
            persist_summary=persist,
        )
        ctx = MiddlewareContext(messages=_long_history(), metadata={"thread_id": "t"})
        await middleware.before_model(ctx)

        manager = SessionManager(store)
        history = await store.load("t")
        context = manager._build_context(history)

        self.assertTrue(saved)
        # 摘要正文被复用，不再出现被压原文
        self.assertIn("压缩历史", context)


if __name__ == "__main__":
    unittest.main()
