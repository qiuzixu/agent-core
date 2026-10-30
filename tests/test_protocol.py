"""protocol/ 公共协议的回归测试。"""

from __future__ import annotations

import unittest

from agent_core.protocol import ApprovalRecord, RunContext, RunEvent


class ProtocolRuntimeTests(unittest.TestCase):
    """验证协议字段序列化与运行时兜底校验。"""

    def test_run_event_schema_version_roundtrip(self) -> None:
        """事件协议版本应能默认为 1，并在序列化后保留。"""
        event = RunEvent(
            event_type="run_started",
            run_id="run-1",
            thread_id="thread-1",
            sequence=0,
        )
        self.assertEqual(event.schema_version, 1)
        restored = RunEvent.from_dict(event.to_dict())
        self.assertEqual(restored.schema_version, 1)

    def test_run_context_schema_version_roundtrip(self) -> None:
        """上下文协议版本应能默认为 1，并在序列化后保留。"""
        context = RunContext(thread_id="thread-1", run_id="run-1")
        self.assertEqual(context.schema_version, 1)
        restored = RunContext.from_dict(context.to_dict())
        self.assertEqual(restored.schema_version, 1)

    def test_run_context_rejects_invalid_status(self) -> None:
        """非法运行状态应在构造时立即失败。"""
        with self.assertRaises(ValueError):
            RunContext(thread_id="thread-1", run_id="run-1", status="what_ever")

    def test_approval_record_rejects_invalid_status(self) -> None:
        """非法审批状态应在构造时立即失败。"""
        with self.assertRaises(ValueError):
            ApprovalRecord(
                thread_id="thread-1",
                run_id="run-1",
                action="dangerous_tool",
                status="what_ever",
            )


if __name__ == "__main__":
    unittest.main()
