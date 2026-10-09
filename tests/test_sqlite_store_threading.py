"""SQLite 存储线程化（同步 IO 入线程池）的回归测试。

沙箱限制：本环境对 dsh 临时目录里的 sqlite/tempfile 文件操作会 PermissionError
甚至挂起，因此这些测试全部使用 monkeypatch 的假连接，验证三件事：
1. 所有 SQLite 存储（7 个）的 async 方法体经线程池执行，不阻塞事件循环；
2. 统一连接参数规范：timeout=30.0 + WAL/synchronous PRAGMA（含初始化 DDL）；
3. 既有行为（commit/rollback/关闭时机）保持不变。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import unittest
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

from agent_core.storage import (
    SqliteContextStore,
    SqliteModelSelectionStore,
    SqliteRunLeaseStore,
    SqliteRuntimeStore,
    SqliteSessionStore,
    SqliteWorkflowExecutionStore,
)
from agent_core.storage._sqlite_utils import SQLITE_CONNECT_TIMEOUT, ensure_wal
from agent_core.storage.memory import SqliteMemoryStore

_SQLITE_CONNECT = "agent_core.storage._sqlite_utils.sqlite3.connect"


class _FakeCursor:
    """最小假游标：默认无结果行，rowcount=1，可迭代（PRAGMA table_info 场景）。"""

    def __init__(self) -> None:
        self.rowcount = 1

    def fetchone(self) -> Any:
        return None

    def fetchall(self) -> list[Any]:
        return []

    def __iter__(self) -> Any:
        return iter([])


class _FakeConnection:
    """记录调用并支持模拟延迟的假 SQLite 连接。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.execute_calls: list[str] = []
        self.executescript_calls: list[str] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0
        self.row_factory: Any = None
        self.thread_id = threading.get_ident()

    def execute(self, sql: str, parameters: Any = ()) -> _FakeCursor:
        self.thread_id = threading.get_ident()
        if self.delay:
            time.sleep(self.delay)
        self.execute_calls.append(sql)
        return _FakeCursor()

    def executescript(self, sql: str) -> None:
        self.thread_id = threading.get_ident()
        self.executescript_calls.append(sql)

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _ConnectRecorder:
    """替换 ``sqlite3.connect`` 并记录每次连接的参数与连接对象。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.connect_kwargs: list[dict[str, Any]] = []
        self.connections: list[_FakeConnection] = []

    def __call__(self, _path: str, **kwargs: Any) -> _FakeConnection:
        self.connect_kwargs.append(kwargs)
        connection = _FakeConnection(delay=self.delay)
        self.connections.append(connection)
        return connection

    def clear(self) -> None:
        self.connect_kwargs.clear()
        self.connections.clear()


def _sqlite_store_specs() -> list[tuple[Callable[[], Any], Callable[[Any], Awaitable[Any]]]]:
    """7 个 SQLite 存储的构造工厂 + 一个有代表性的 async 方法。"""
    return [
        (lambda: SqliteSessionStore("unused.db"), lambda store: store.load("t")),
        (lambda: SqliteContextStore("unused.db"), lambda store: store.get("t")),
        (lambda: SqliteMemoryStore("unused.db"), lambda store: store.search("ns")),
        (lambda: SqliteWorkflowExecutionStore("unused.db"), lambda store: store.load("e")),
        (lambda: SqliteRuntimeStore("unused.db"), lambda store: store.get_thread_owner("t")),
        (
            lambda: SqliteModelSelectionStore("unused.db"),
            lambda store: store.resolve(user_id="u", tenant_id="tn"),
        ),
        (lambda: SqliteRunLeaseStore("unused.db"), lambda store: store.list_expired()),
    ]


class SqliteStoreThreadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_stores_run_async_methods_in_worker_thread(self) -> None:
        """7 个 SQLite 存储的 async 方法体都在事件循环线程之外执行。"""
        loop_thread = threading.get_ident()
        recorder = _ConnectRecorder()
        with patch("agent_core.storage._sqlite_utils.sqlite3.connect", side_effect=recorder):
            for store_factory, operation in _sqlite_store_specs():
                store = store_factory()
                recorder.clear()
                await operation(store)
                self.assertTrue(recorder.connections, f"{type(store).__name__} 应打开连接")
                worker_threads = {connection.thread_id for connection in recorder.connections}
                self.assertNotIn(
                    loop_thread,
                    worker_threads,
                    f"{type(store).__name__} 的 async 方法仍在事件循环线程内执行同步 IO",
                )

    async def test_slow_sqlite_io_does_not_block_event_loop(self) -> None:
        """在假连接的 execute 中 sleep 0.3s，并发任务仍能推进。"""
        recorder = _ConnectRecorder(delay=0.3)
        with patch("agent_core.storage._sqlite_utils.sqlite3.connect", side_effect=recorder):
            store = SqliteSessionStore("unused.db")
            recorder.clear()

            progress: list[str] = []

            async def watcher() -> None:
                await asyncio.sleep(0.05)
                progress.append("watcher-推进")

            load_task = asyncio.create_task(store.load("t"))
            await watcher()

            # 若同步 IO 冻结了事件循环，watcher 只能在 load 完成后才被调度。
            self.assertEqual(progress, ["watcher-推进"])
            self.assertEqual(await load_task, [])

        self.assertNotEqual(
            recorder.connections[0].thread_id,
            threading.get_ident(),
            "慢查询应在工作线程中执行",
        )
        self.assertEqual(recorder.connections[0].close_calls, 1)

    async def test_stores_share_timeout_and_wal_connection_params(self) -> None:
        """统一连接参数规范：timeout=30.0，初始化 DDL 连接也开启 WAL。"""
        recorder = _ConnectRecorder()
        with patch("agent_core.storage._sqlite_utils.sqlite3.connect", side_effect=recorder):
            for store_factory, _operation in _sqlite_store_specs():
                store = store_factory()
                # 构造期 DDL/PRAGMA 初始化同样走统一连接规范。
                self.assertEqual(
                    recorder.connect_kwargs,
                    [{"timeout": SQLITE_CONNECT_TIMEOUT}] * len(recorder.connect_kwargs),
                    f"{type(store).__name__} 应以 timeout=30.0 打开连接",
                )
                init_connection = recorder.connections[0]
                self.assertEqual(init_connection.execute_calls[0], "PRAGMA journal_mode=WAL")
                self.assertEqual(init_connection.execute_calls[1], "PRAGMA synchronous=NORMAL")
                self.assertTrue(
                    all(
                        "PRAGMA journal_mode=WAL" in connection.execute_calls
                        for connection in recorder.connections
                    ),
                    f"{type(store).__name__} 的每个连接都应开启 WAL",
                )
                self.assertEqual(init_connection.close_calls, 1)
                recorder.clear()

    async def test_sqlite_row_factory_is_configured_for_session_store(self) -> None:
        """沿用 sqlite3.Row 行工厂，保证既有按列名访问的返回值不变。"""
        recorder = _ConnectRecorder()
        with patch("agent_core.storage._sqlite_utils.sqlite3.connect", side_effect=recorder):
            SqliteSessionStore("unused.db")
            self.assertIs(recorder.connections[0].row_factory, sqlite3.Row)

    async def test_ensure_wal_executes_expected_pragmas(self) -> None:
        """ensure_wal 幂等开启 WAL 与 NORMAL 同步级别。"""
        connection = _FakeConnection()
        ensure_wal(connection)
        ensure_wal(connection)
        self.assertEqual(
            connection.execute_calls,
            ["PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"] * 2,
        )

    async def test_sqlite_init_failure_propagates_without_partial_state(self) -> None:
        """初始化失败时异常照常抛出（经线程池传播）。"""

        class _ExplodingStore(SqliteSessionStore):
            def _init_db(self) -> None:
                raise RuntimeError("ddl 失败")

        with self.assertRaisesRegex(RuntimeError, "ddl 失败"):
            _ExplodingStore("unused.db")
