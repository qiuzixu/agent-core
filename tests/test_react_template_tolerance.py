"""memory_context 模板容错回归测试。

覆盖两类历史事故：
1. 旧版 prompts.json 使用 {history} 占位符（代码已改用 {memory_context}），
   旧数据加载后 /context/usage 抛 KeyError: 'history'；
2. 模板含未知占位符或字面花括号，不应让对话链路崩溃。
"""

from __future__ import annotations

import unittest

from agent_core import ToolExecutor, ToolRegistry
from agent_core.runtime.react import ReActAgent, _SafeFormatDict


class _UsageOnlyModel:
    """只为 context_usage 提供返回值的假模型。"""

    async def context_usage(self, messages, **kwargs):
        return None


class SafeFormatTemplateTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self, template: str) -> ReActAgent:
        registry = ToolRegistry()
        registry.register("lookup", lambda query: query, "查询")
        return ReActAgent(
            _UsageOnlyModel(),
            ToolExecutor(registry),
            system_prompt="测试助手",
            tool_definitions=[],
            memory_context_template=template,
        )

    async def test_legacy_history_placeholder_does_not_crash(self):
        """旧版 {history} 占位符等价映射到 memory_context，不再 KeyError。"""
        agent = self._agent("历史：{history}\n问题：{user_input}")
        msgs = agent._build_initial_messages("问题", "记忆内容")
        self.assertEqual(len(msgs), 2)
        self.assertIn("历史：记忆内容", msgs[1].content)
        self.assertIn("问题：问题", msgs[1].content)

    async def test_unknown_placeholder_kept_literal(self):
        """未知占位符原样保留，不抛异常。"""
        agent = self._agent("记忆：{memory_context} 字面：{no_such_key}")
        msgs = agent._build_initial_messages("q", "m")
        self.assertIn("{no_such_key}", msgs[1].content)
        self.assertIn("m", msgs[1].content)

    async def test_safe_format_dict_unit(self):
        """_SafeFormatDict 缺失键返回 "{key}" 原文。"""
        d = _SafeFormatDict(a="A")
        self.assertEqual(d["a"], "A")
        self.assertEqual(d["missing"], "{missing}")


if __name__ == "__main__":
    unittest.main()
