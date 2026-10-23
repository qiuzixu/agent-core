"""runtime compat 层的通用值对象和消息解析回归测试。"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime

from agent_core.protocol.messages import assistant_message, user_message
from agent_core.protocol.runtime import RunContext
from agent_core.runtime.compat import (
    RunRecord,
    ThreadRecord,
    content_text,
    extract_prompt,
    messages_json,
    now_iso,
)


class RuntimeCompatTests(unittest.IsolatedAsyncioTestCase):
    def test_now_iso_is_utc_iso8601(self) -> None:
        """时间字段统一使用 UTC ISO 8601，便于 API 和排序消费。"""
        parsed = datetime.fromisoformat(now_iso())
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_messages_json_converts_core_messages(self) -> None:
        """批量转换应保持 OpenAI 兼容响应结构。"""
        value = messages_json([user_message("你好"), assistant_message("在的")])
        self.assertEqual(value, [{"role":"user","content":"你好"},{"role":"assistant","content":"在的"}])

    def test_thread_record_state_helpers(self) -> None:
        record = ThreadRecord(thread_id="thread-1")
        record.updated_at = "2000-01-01T00:00:00+00:00"

        record.mark_running()
        self.assertEqual(record.status, "running")
        self.assertNotEqual(record.updated_at, "2000-01-01T00:00:00+00:00")

        record.set_interrupt({"kind": "approval"}, "请确认")
        self.assertEqual(record.interrupt, {"kind": "approval"})
        self.assertEqual(record.pending_prompt, "请确认")

        record.set_interrupt(None)
        self.assertIsNone(record.interrupt)
        self.assertIsNone(record.pending_prompt)

        record.mark_idle()
        self.assertEqual(record.status, "idle")

    def test_thread_record_to_dict(self) -> None:
        record = ThreadRecord(thread_id="thread-1", user_id="u1", tenant_id="t1")
        value = record.to_dict()
        self.assertEqual(value["thread_id"], "thread-1")
        self.assertEqual(value["user_id"], "u1")
        self.assertEqual(value["tenant_id"], "t1")
        self.assertEqual(value["status"], "idle")

    async def test_run_record_to_summary_hides_task(self) -> None:
        """对外摘要不能暴露 asyncio.Task 这种进程内执行句柄。"""

        async def runner() -> dict[str, str]:
            return {"ok": "true"}

        task = asyncio.create_task(runner())
        record = RunRecord(
            run_id="run-1",
            thread_id="thread-1",
            task=task,
            context=RunContext(thread_id="thread-1", run_id="run-1"),
            status="completed",
            result={"answer": "ok"},
        )
        summary = record.to_summary()
        self.assertEqual(summary["run_id"], "run-1")
        self.assertEqual(summary["thread_id"], "thread-1")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["result"], {"answer": "ok"})
        self.assertNotIn("task", summary)
        self.assertNotIn("context", summary)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_content_text_supports_common_blocks(self) -> None:
        content = [
            {"type": "input_text", "text": "第一段"},
            {"type": "image_url", "image_url": "ignored"},
            {"type": "output_text", "text": "第二段"},
            {"content": "旧格式"},
        ]
        self.assertEqual(content_text(content), "第一段\n第二段\n旧格式")

    def test_extract_prompt_supports_common_inputs(self) -> None:
        self.assertEqual(extract_prompt("直接输入"), "直接输入")
        self.assertEqual(
            extract_prompt({"messages": [{"role": "user", "content": "消息"}]}),
            "消息",
        )
        self.assertEqual(
            extract_prompt({
                "input": {
                    "messages": [
                        {"role": "system", "content": "系统"},
                        {"role": "user", "content": [{"type": "input_text", "text": "结构化"}]},
                    ]
                }
            }),
            "结构化",
        )
        self.assertEqual(
            extract_prompt([{"role": "user", "content": "数组消息"}]),
            "数组消息",
        )


if __name__ == "__main__":
    unittest.main()
