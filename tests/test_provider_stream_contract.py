"""Provider 流式输出契约测试（review B 节第 7 条）。

用假客户端固化三家适配器流式结束块的行为，防止实现漂移：
- OpenAI 兼容接口：结束块携带上游最后一个 choice 的 ``finish_reason``
  （文本路径 "stop"、工具路径 "tool_calls"）；
- Anthropic / Gemini：现状是结束块 ``finish_reason=None``，文本增量与
  工具调用归并行为与本文件断言一致。

全部使用内存假客户端，不依赖任何模型 SDK。
"""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from agent_core.model.providers.anthropic import AnthropicProvider
from agent_core.model.providers.gemini import GeminiProvider
from agent_core.model.providers.openai_compatible import OpenAIProvider
from agent_core.protocol import user_message


async def _collect(stream: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in stream]


# ──────────────────────────────────────────────
# OpenAI 兼容接口假客户端
# ──────────────────────────────────────────────


def _openai_text_chunk(content: str, finish_reason: str | None = None) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


def _openai_tool_delta(
    index: int,
    *,
    call_id: str = "",
    name: str = "",
    arguments: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        id=call_id or None,
        function=SimpleNamespace(name=name or None, arguments=arguments or None),
    )


def _openai_tool_chunk(
    tool_deltas: list[SimpleNamespace],
    finish_reason: str | None = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(content=None, tool_calls=tool_deltas)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


def _openai_provider(chunks: list[SimpleNamespace]) -> OpenAIProvider:
    provider = object.__new__(OpenAIProvider)

    # create() 返回一个 awaitable，其结果支持 async for（模拟 SDK 流对象）。
    async def create(**kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        assert kwargs.get("stream") is True

        async def gen() -> AsyncIterator[SimpleNamespace]:
            for chunk in chunks:
                yield chunk

        return gen()

    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider._model = "fake-model"
    provider._temperature = 0.0
    provider._max_tokens = 64
    return provider


class TestOpenAiStreamContract(unittest.IsolatedAsyncioTestCase):
    async def test_text_stream_ends_with_finish_reason_stop(self) -> None:
        """纯文本流：结束块携带上游最后的 finish_reason="stop"。"""
        provider = _openai_provider(
            [
                _openai_text_chunk("你好"),
                _openai_text_chunk("，世界"),
                _openai_text_chunk(None, finish_reason="stop"),
            ]
        )
        chunks = [chunk async for chunk in provider.stream_chat([user_message("你好")])]

        self.assertEqual([(c.text, c.tool_calls) for c in chunks[:-1]], [("你好", []), ("，世界", [])])
        final = chunks[-1]
        self.assertEqual(final.text, "")
        self.assertFalse(final.is_tool_call)
        self.assertEqual(final.finish_reason, "stop")

    async def test_tool_call_stream_ends_with_finish_reason_tool_calls(self) -> None:
        """工具调用流：参数增量按 index 归并，结束块携带 finish_reason="tool_calls"。"""
        provider = _openai_provider(
            [
                _openai_tool_chunk([_openai_tool_delta(0, call_id="call-1")]),
                _openai_tool_chunk([_openai_tool_delta(0, name="lookup")]),
                _openai_tool_chunk([_openai_tool_delta(0, arguments='{"que')]),
                _openai_tool_chunk(
                    [_openai_tool_delta(0, arguments='ry": "机场"}')],
                    finish_reason="tool_calls",
                ),
            ]
        )
        chunks = [chunk async for chunk in provider.stream_chat([user_message("查询")])]

        self.assertEqual(len(chunks), 1)
        final = chunks[0]
        self.assertTrue(final.is_tool_call)
        self.assertEqual(final.finish_reason, "tool_calls")
        self.assertEqual(
            final.tool_calls,
            [{"id": "call-1", "type": "function", "name": "lookup", "args": {"query": "机场"}}],
        )


# ──────────────────────────────────────────────
# Anthropic 假客户端
# ──────────────────────────────────────────────


class _FakeAnthropicStream:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self._events = events

    async def __aenter__(self) -> _FakeAnthropicStream:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    def __aiter__(self) -> AsyncIterator[SimpleNamespace]:
        async def gen() -> AsyncIterator[SimpleNamespace]:
            for event in self._events:
                yield event

        return gen()


def _anthropic_provider(events: list[SimpleNamespace]) -> AnthropicProvider:
    provider = object.__new__(AnthropicProvider)
    provider._client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **kwargs: _FakeAnthropicStream(events))
    )
    provider.model = "fake-model"
    provider._temperature = 0.0
    provider._max_tokens = 64
    provider._context_window_tokens = None
    provider.provider_name = "anthropic"
    return provider


class TestAnthropicStreamContract(unittest.IsolatedAsyncioTestCase):
    async def test_stream_forwards_text_and_merges_tool_calls_without_finish_reason(self) -> None:
        """现状契约：文本增量逐块转发；工具参数增量归并；结束块 finish_reason 为 None。"""
        events = [
            SimpleNamespace(delta=SimpleNamespace(text="你好")),
            SimpleNamespace(
                content_block=SimpleNamespace(type="tool_use", id="tu_1", name="lookup"),
                index=0,
            ),
            SimpleNamespace(delta=SimpleNamespace(partial_json='{"query": "机'), index=0),
            SimpleNamespace(delta=SimpleNamespace(partial_json='场"}'), index=0),
        ]
        provider = _anthropic_provider(events)

        chunks = [chunk async for chunk in provider.stream_chat([user_message("查询")])]

        self.assertEqual(chunks[0].text, "你好")
        final = chunks[-1]
        self.assertTrue(final.is_tool_call)
        self.assertEqual(final.text, "")
        # 现状：Anthropic 结束块不填充 finish_reason（与 OpenAI 不同）。
        self.assertIsNone(final.finish_reason)
        self.assertEqual(
            final.tool_calls,
            [{"id": "tu_1", "type": "function", "name": "lookup", "args": {"query": "机场"}}],
        )

    async def test_text_only_stream_ends_with_empty_tool_call_chunk(self) -> None:
        events = [
            SimpleNamespace(delta=SimpleNamespace(text="部分")),
            SimpleNamespace(delta=SimpleNamespace(text="文本")),
        ]
        provider = _anthropic_provider(events)

        chunks = [chunk async for chunk in provider.stream_chat([user_message("你好")])]

        self.assertEqual([c.text for c in chunks], ["部分", "文本", ""])
        self.assertFalse(chunks[-1].is_tool_call)
        self.assertIsNone(chunks[-1].finish_reason)


# ──────────────────────────────────────────────
# Gemini 假客户端
# ──────────────────────────────────────────────


class _FakePart:
    def __init__(
        self,
        text: str | None = None,
        function_call: SimpleNamespace | None = None,
    ) -> None:
        self.text = text
        self.function_call = function_call

    @staticmethod
    def from_text(text: str) -> _FakePart:
        return _FakePart(text=text)

    @staticmethod
    def from_function_call(name: str, args: dict[str, Any]) -> _FakePart:
        return _FakePart(function_call=SimpleNamespace(name=name, args=args, id=""))

    @staticmethod
    def from_function_response(name: str, response: dict[str, Any]) -> _FakePart:
        del name, response
        return _FakePart()


class _FakeContent:
    def __init__(self, role: str, parts: list[_FakePart]) -> None:
        self.role = role
        self.parts = parts


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _FakeGeminiTypes:
    Content = _FakeContent
    Part = _FakePart
    GenerateContentConfig = _FakeGenerateContentConfig


class _FakeGeminiModels:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    async def generate_content_stream(self, *, model: str, contents: Any, config: Any) -> AsyncIterator[Any]:
        del model, contents, config

        async def gen() -> AsyncIterator[Any]:
            for chunk in self._chunks:
                yield chunk

        return gen()


def _gemini_response(parts: list[_FakePart]) -> SimpleNamespace:
    candidate = SimpleNamespace(content=SimpleNamespace(parts=parts))
    return SimpleNamespace(candidates=[candidate])


def _gemini_provider(chunks: list[SimpleNamespace]) -> GeminiProvider:
    provider = object.__new__(GeminiProvider)
    provider._genai_types = _FakeGeminiTypes
    provider._client = SimpleNamespace(
        aio=SimpleNamespace(models=_FakeGeminiModels(chunks)),
    )
    provider.model = "fake-model"
    provider._temperature = 0.0
    provider._max_tokens = 64
    provider._context_window_tokens = None
    provider.provider_name = "gemini"
    return provider


class TestGeminiStreamContract(unittest.IsolatedAsyncioTestCase):
    async def test_stream_merges_function_calls_and_leaves_finish_reason_empty(self) -> None:
        """现状契约：function_call 增量按位置合并、id 自动补齐；结束块 finish_reason 为 None。"""
        provider = _gemini_provider(
            [
                SimpleNamespace(
                    candidates=[
                        SimpleNamespace(content=_FakeContent("model", [_FakePart.from_text("你好")]))
                    ]
                ),
                SimpleNamespace(
                    candidates=[
                        SimpleNamespace(
                            content=_FakeContent(
                                "model",
                                [_FakePart.from_function_call("lookup", {"query": "机场"})],
                            )
                        )
                    ]
                ),
                SimpleNamespace(
                    candidates=[
                        SimpleNamespace(
                            content=_FakeContent(
                                "model",
                                [_FakePart.from_function_call("lookup", {"limit": 5})],
                            )
                        )
                    ]
                ),
            ]
        )

        chunks = [chunk async for chunk in provider.stream_chat([user_message("查询")])]

        self.assertEqual(chunks[0].text, "你好")
        final = chunks[-1]
        self.assertTrue(final.is_tool_call)
        # 现状：Gemini 结束块不填充 finish_reason。
        self.assertIsNone(final.finish_reason)
        self.assertEqual(len(final.tool_calls), 1)
        merged = final.tool_calls[0]
        self.assertEqual(merged["type"], "function")
        self.assertEqual(merged["name"], "lookup")
        # 跨块的 args 字典按 key 合并（Gemini 每块回传完整字段值）。
        self.assertEqual(merged["args"], {"query": "机场", "limit": 5})
        # Gemini 不回传调用 id，适配器自动生成非空 id。
        self.assertTrue(merged["id"])

    async def test_text_only_stream_ends_with_empty_chunk(self) -> None:
        provider = _gemini_provider(
            [
                SimpleNamespace(
                    candidates=[
                        SimpleNamespace(content=_FakeContent("model", [_FakePart.from_text("文本")]))
                    ]
                )
            ]
        )

        chunks = [chunk async for chunk in provider.stream_chat([user_message("你好")])]

        self.assertEqual([c.text for c in chunks], ["文本", ""])
        self.assertFalse(chunks[-1].is_tool_call)
        self.assertIsNone(chunks[-1].finish_reason)


if __name__ == "__main__":
    unittest.main()
