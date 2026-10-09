"""TimeTravel 文件名严格清洗与版本容量上限的回归测试。

沙箱限制：dsh 临时目录里的文件操作会 PermissionError/挂起，因此这些测试
不触碰真实文件系统——清洗函数是纯函数；文件路径映射用跳过 mkdir 的测试替身；
版本淘汰用 ``storage_dir=None`` 的纯内存模式（``_persist_versions`` 自然退化为
no-op，必要时以假持久化函数替换验证收缩行为）。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_core.checkpoint.store import (
    CheckpointError,
    FileCheckpointer,
    TimeTravelCheckpointer,
    _restore_thread_id,
    _sanitize_thread_id,
)


class _NoDirFileCheckpointer(FileCheckpointer):
    """不真正创建目录的测试替身（仅验证文件名映射，不触碰文件系统）。"""

    def __init__(self, storage_dir: str) -> None:
        self._storage_dir = Path(storage_dir)


class _NoDirTimeTravelCheckpointer(TimeTravelCheckpointer):
    """不真正创建目录的测试替身。"""

    def __init__(self, storage_dir: str, *, max_versions_per_thread: int = 100) -> None:
        self._storage_dir = Path(storage_dir)
        self._versions: dict[str, list[object]] = {}
        self._current_version: dict[str, int] = {}
        self._max_versions_per_thread = max_versions_per_thread


class ThreadIdSanitizationTests(unittest.TestCase):
    def test_sanitize_is_injective_and_readable(self) -> None:
        """白名单字符保留可读性；a/b 与 a_b 清洗结果不同，不再互覆。"""
        self.assertEqual(_sanitize_thread_id("a_b"), "a_b")
        self.assertEqual(_sanitize_thread_id("a/b"), "a%2Fb")
        self.assertEqual(_sanitize_thread_id("a\\b"), "a%5Cb")
        self.assertNotEqual(_sanitize_thread_id("a/b"), _sanitize_thread_id("a_b"))
        # 纯白名单 thread 保持原样
        self.assertEqual(_sanitize_thread_id("thread-1.2_x"), "thread-1.2_x")

    def test_sanitize_handles_windows_illegal_and_control_chars(self) -> None:
        """Windows 非法字符与控制符必须被转义。"""
        for illegal in '<>:"|?*':
            sanitized = _sanitize_thread_id(f"a{illegal}b")
            self.assertNotIn(illegal, sanitized)
            self.assertIn("%", sanitized)
        self.assertEqual(_sanitize_thread_id("thread:1"), "thread%3A1")
        self.assertNotIn("\x01", _sanitize_thread_id("ctrl\x01id"))
        self.assertEqual(_sanitize_thread_id("ctrl\x01id"), "ctrl%01id")

    def test_sanitize_escapes_percent_to_stay_reversible(self) -> None:
        """``%`` 本身必须转义，否则 a/b 与 a%2Fb 会清洗出同名文件。"""
        self.assertEqual(_sanitize_thread_id("a%2Fb"), "a%252Fb")
        self.assertNotEqual(_sanitize_thread_id("a%2Fb"), _sanitize_thread_id("a/b"))

    def test_sanitize_handles_trailing_dot_space_and_reserved_names(self) -> None:
        """结尾点/空格与 Windows 保留设备名不直接落地（会被系统特殊处理）。"""
        self.assertNotEqual(_sanitize_thread_id("dot."), "dot.")
        self.assertNotEqual(_sanitize_thread_id("space "), "space ")
        self.assertNotEqual(_sanitize_thread_id("CON"), "CON")
        self.assertNotEqual(_sanitize_thread_id("aux.txt"), "aux.txt")

    def test_sanitize_restore_roundtrip(self) -> None:
        """清洗与逆映射互逆：list_threads 反向映射可以还原 thread_id。"""
        nasty_ids = [
            "a/b",
            "a_b",
            "a\\b",
            "thread:1",
            'a<b>c:d"e|f?g*h',
            "con",
            "aux.txt",
            "nul",
            "dot.",
            "space ",
            "中文/thread",
            "ctrl\x01id",
            "\x7f",
            "% literal",
            "%2F",
            "..",
            ".",
        ]
        for original in nasty_ids:
            with self.subTest(thread_id=original):
                self.assertEqual(_restore_thread_id(_sanitize_thread_id(original)), original)

    def test_restore_returns_unrecognized_stem_as_is(self) -> None:
        """历史遗留文件名无法还原时原样返回，不抛异常。"""
        self.assertEqual(_restore_thread_id("plain-thread"), "plain-thread")


class FileCheckpointerPathTests(unittest.TestCase):
    def test_get_file_path_keeps_distinct_threads_distinct(self) -> None:
        """a/b 与 a_b 映射到不同文件，不再互相覆盖；含 : 的 thread 可用。"""
        checkpointer = _NoDirFileCheckpointer("unused")
        self.assertEqual(checkpointer._get_file_path("a/b").name, "a%2Fb.json")
        self.assertEqual(checkpointer._get_file_path("a_b").name, "a_b.json")
        self.assertNotEqual(checkpointer._get_file_path("a/b"), checkpointer._get_file_path("a_b"))
        self.assertEqual(checkpointer._get_file_path("thread:1").name, "thread%3A1.json")
        self.assertEqual(checkpointer._get_file_path("a\\b").name, "a%5Cb.json")

    def test_time_travel_version_file_uses_same_sanitization(self) -> None:
        checkpointer = _NoDirTimeTravelCheckpointer("unused")
        self.assertEqual(checkpointer._get_version_file("a/b").name, "a%2Fb_versions.json")
        self.assertEqual(checkpointer._get_version_file("thread:1").name, "thread%3A1_versions.json")
        self.assertNotEqual(
            checkpointer._get_version_file("a/b"),
            checkpointer._get_version_file("a_b"),
        )


class TimeTravelVersionCapTests(unittest.IsolatedAsyncioTestCase):
    async def test_version_cap_evicts_oldest_and_keeps_pointer_on_newest(self) -> None:
        """追加超过上限时淘汰最旧版本；保存后指针仍指向新版本。"""
        checkpointer = TimeTravelCheckpointer(max_versions_per_thread=3)
        ids = [await checkpointer.save("t", {"step": index}) for index in range(5)]

        listed = await checkpointer.list_versions("t")
        self.assertEqual([item["version_id"] for item in listed], ids[2:])
        self.assertEqual(await checkpointer.load("t"), {"step": 4})
        # 被淘汰的版本无法再读取
        self.assertIsNone(await checkpointer.load("t", ids[0]))

    async def test_version_cap_default_is_100(self) -> None:
        """默认上限 100：保存 105 个版本后只保留最近 100 个。"""
        checkpointer = TimeTravelCheckpointer()
        ids = [await checkpointer.save("t", {"step": index}) for index in range(105)]

        listed = await checkpointer.list_versions("t")
        self.assertEqual(len(listed), 100)
        self.assertEqual([item["version_id"] for item in listed], ids[5:])
        self.assertEqual(await checkpointer.load("t"), {"step": 104})

    async def test_persisted_version_list_shrinks_with_cap(self) -> None:
        """持久化内容同步收缩：替换 _persist_versions 为记录型假实现。"""
        checkpointer = TimeTravelCheckpointer(max_versions_per_thread=3)
        persisted_sizes: list[int] = []

        async def spy_persist(thread_id: str) -> None:
            del thread_id
            persisted_sizes.append(len(checkpointer._versions["t"]))

        checkpointer._persist_versions = spy_persist

        for index in range(6):
            await checkpointer.save("t", {"step": index})

        self.assertEqual(persisted_sizes, [1, 2, 3, 3, 3, 3])

    async def test_cap_rejects_non_positive_limit(self) -> None:
        with self.assertRaises(ValueError):
            TimeTravelCheckpointer(max_versions_per_thread=0)

    async def test_persist_failure_restores_pointer_to_successor_of_evicted(self) -> None:
        """持久化失败时按身份恢复指针；被淘汰的当前版本指针前移到继任版本。"""
        checkpointer = TimeTravelCheckpointer(max_versions_per_thread=2)
        first = await checkpointer.save("t", {"step": 1})
        second = await checkpointer.save("t", {"step": 2})
        self.assertTrue(await checkpointer.rollback("t", first))

        async def failing_persist(thread_id: str) -> None:
            del thread_id
            raise CheckpointError("disk full")

        checkpointer._persist_versions = failing_persist
        with self.assertRaises(CheckpointError):
            await checkpointer.save("t", {"step": 3})

        # first 已被容量淘汰；失败的版本被移除；指针前移到继任的 second。
        self.assertEqual(await checkpointer.load("t"), {"step": 2})
        listed = await checkpointer.list_versions("t")
        self.assertEqual([item["version_id"] for item in listed], [second])

    async def test_persist_failure_without_eviction_keeps_previous_pointer(self) -> None:
        """未触发淘汰时，持久化失败恢复到保存前指针（原语义保持不变）。"""
        checkpointer = TimeTravelCheckpointer(max_versions_per_thread=100)
        first = await checkpointer.save("t", {"step": 1})
        second = await checkpointer.save("t", {"step": 2})
        self.assertTrue(await checkpointer.rollback("t", first))

        async def failing_persist(thread_id: str) -> None:
            del thread_id
            raise CheckpointError("disk full")

        checkpointer._persist_versions = failing_persist
        with self.assertRaises(CheckpointError):
            await checkpointer.save("t", {"step": 3})

        self.assertEqual(await checkpointer.load("t"), {"step": 1})
        self.assertEqual(
            [item["version_id"] for item in await checkpointer.list_versions("t")],
            [first, second],
        )
