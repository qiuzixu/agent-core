"""SQLite 存储共享工具：统一连接参数规范与线程池执行包装。

sqlite3 驱动是同步实现；写锁等待（busy timeout）会冻结所在线程。
存储层保持"每操作开新连接"的简单模型，但所有 async 方法体统一经
`_run_in_thread` 放入工作线程执行，把可能的锁等待隔离在事件循环之外。

统一连接参数规范（各存储的 `_connect` 必须经 `sqlite_connection` 复用）：

- ``timeout=30.0``：写锁等待上限 30 秒，超时抛 ``sqlite3.OperationalError``；
- `ensure_wal`：WAL 是库级持久属性（保存在数据库文件中），在存储初始化
  建连接时开启一次即可，之后的连接自动继承。`sqlite_connection` 每次建连
  都幂等调用 `ensure_wal`，因此初始化与后续每操作连接使用同一规范。

构造期 DDL / PRAGMA 初始化同样入线程：构造函数是同步入口、无法 ``await``，
统一经 `run_sync_in_thread` 在一次性工作线程中执行后返回。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = [
    "SQLITE_CONNECT_TIMEOUT",
    "_run_in_thread",
    "ensure_wal",
    "run_sync_in_thread",
    "sqlite_connection",
]

# 所有 SQLite 存储统一使用的写锁等待上限（秒）。
SQLITE_CONNECT_TIMEOUT = 30.0

# 连接级回调签名：与 sqlite3.Connection.row_factory 的类型契约一致。
RowFactory = Callable[[sqlite3.Cursor, tuple[Any, ...]], Any]


def ensure_wal(conn: sqlite3.Connection) -> None:
    """开启 WAL 日志模式与 NORMAL 同步级别（幂等，可重复调用）。

    WAL 是库级属性：一经设置即持久保存在数据库文件中，后续连接自动继承；
    ``synchronous`` 是连接级属性，因此每次建连都需要重新设置。
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")


@contextmanager
def sqlite_connection(
    db_path: str | Path,
    *,
    row_factory: RowFactory | None = None,
) -> Generator[sqlite3.Connection]:
    """按统一参数规范打开一个自动提交/回滚的 SQLite 连接（每操作一个）。

    - ``timeout=30.0``：统一写锁等待上限；
    - `ensure_wal`：统一开启 WAL 与 ``synchronous=NORMAL``。

    退出时提交；异常时回滚并原样抛出；连接在 finally 中关闭。
    """
    conn = sqlite3.connect(str(db_path), timeout=SQLITE_CONNECT_TIMEOUT)
    try:
        if row_factory is not None:
            conn.row_factory = row_factory
        ensure_wal(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def _run_in_thread[T](func: Callable[[], T]) -> T:
    """把同步 SQLite IO 放到默认线程池执行，避免锁等待冻结事件循环。"""
    return await asyncio.to_thread(func)


def run_sync_in_thread[T](func: Callable[[], T]) -> T:
    """在一次性工作线程中执行同步初始化（DDL/PRAGMA）并等待完成。

    存储构造函数是同步入口，无法 ``await``；初始化放入独立线程执行，
    与 async 方法共用同一连接参数规范。
    """
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-core-sqlite-init") as pool:
        return pool.submit(func).result()
