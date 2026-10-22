"""SQLite checkpoint 后端（开发环境默认）。

- 每次 save() 插入一行新版本（不做覆盖），load() 取 created_at 最新的一行；
- WAL 模式：读写不互斥，单实例并发安全；
- 复用 storage/_sqlite_utils 的连接工具，超时/WAL 行为与其余存储模块一致；
- 表按 (thread_id, checkpoint_ns, version_id) 做主键，
  与协议层的 (thread_id, checkpoint_ns) 二级隔离一一对应；
- 保留全部版本历史，通过 prune(keep_latest=N) 手动收敛。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.checkpoint.schema import (
    CHECKPOINT_SCHEMA_VERSION,
    ensure_schema_version,
    get_schema_version,
    migrate_state,
)
from agent_core.checkpoint.store import DEFAULT_CHECKPOINT_NS, _storage_key
from agent_core.errors import CheckpointError
from agent_core.storage._sqlite_utils import (
    _run_in_thread,
    run_sync_in_thread,
    sqlite_connection,
)

logger = logging.getLogger(__name__)

# 表设计：主键 (thread_id, checkpoint_ns, version_id) 同时覆盖
# 谱系隔离（ns）与版本链（version_id）；created_at 索引服务取最新查询。
_DDL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    version_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    state TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    branch TEXT NOT NULL DEFAULT 'main',
    schema_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, version_id)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_thread_ns
ON checkpoints (thread_id, checkpoint_ns, created_at DESC);
"""

# 注意：列顺序与 _row_to_state 的 zip 顺序一一对应，改列必须同步改两处。
_LATEST_COLS = """thread_id, checkpoint_ns, version_id, timestamp, state, metadata, branch, schema_version"""


class SqliteCheckpointer:
    """SQLite (WAL 模式) checkpoint 后端，带版本历史。

    Every save() creates a new version row. load() without version_id
    returns the latest version's state (with schema migration applied).
    list_namespaces / list_versions provide trace_tree support.
    """

    def __init__(self, db_path: str | Path = "./checkpoints.db") -> None:
        """初始化并建表。

        Args:
            db_path: SQLite 数据库文件路径，默认当前目录下的 checkpoints.db。
        """
        self._db_path = str(Path(db_path).resolve())
        run_sync_in_thread(self._init_db)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_DDL)

    @contextmanager
    def _connect(self):
        with sqlite_connection(self._db_path) as conn:
            yield conn

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> str:
        """保存新版本，返回版本 ID。"""
        version_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        stamped = ensure_schema_version(state)
        schema_version = get_schema_version(stamped)

        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO checkpoints
                    (thread_id, checkpoint_ns, version_id, timestamp, state, metadata, branch, schema_version, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        thread_id,
                        checkpoint_ns,
                        version_id,
                        now,
                        json.dumps(stamped, ensure_ascii=False),
                        json.dumps(metadata or {}, ensure_ascii=False),
                        "main",
                        schema_version,
                        now,
                    ),
                )

        try:
            await _run_in_thread(_op)
        except Exception as exc:
            raise CheckpointError(
                f"保存 checkpoint 失败（thread={thread_id!r} ns={checkpoint_ns!r}）：{exc}"
            ) from exc

        logger.debug(
            "已保存 checkpoint %s（thread=%r ns=%r，schema v%d）",
            version_id, thread_id, checkpoint_ns, schema_version,
        )
        return version_id

    async def load(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> dict[str, Any] | None:
        """加载最新版本的 state（带 schema 自动迁移）。

        与 TimeTravelCheckpointer 不同：这里没有显式当前指针，
        始终取 created_at 最新一行；也没有 version_id 参数——
        需要任意版本回退请用 TimeTravelCheckpointer。
        """
        def _op() -> dict[str, Any] | None:
            with self._connect() as conn:
                row = conn.execute(
                    f"""SELECT {_LATEST_COLS} FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ?
                    ORDER BY created_at DESC LIMIT 1""",
                    (thread_id, checkpoint_ns),
                ).fetchone()
            if row is None:
                return None
            return self._row_to_state(row)

        try:
            return await _run_in_thread(_op)
        except CheckpointError:
            raise
        except Exception as exc:
            raise CheckpointError(
                f"加载 checkpoint 失败（thread={thread_id!r} ns={checkpoint_ns!r}）：{exc}"
            ) from exc

    async def delete(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
        """删除该 (thread_id, ns) 下的全部版本。"""
        def _op() -> None:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ?",
                    (thread_id, checkpoint_ns),
                )

        try:
            await _run_in_thread(_op)
        except Exception as exc:
            raise CheckpointError(
                f"删除 checkpoint 失败（thread={thread_id!r} ns={checkpoint_ns!r}）：{exc}"
            ) from exc

    def list_threads(self) -> list[str]:
        """列出所有 thread_id（去重）。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id").fetchall()
        return [row[0] for row in rows]

    def list_namespaces(self, thread_id: str) -> list[str]:
        """列出该 thread 下所有已使用的 checkpoint namespace。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_ns",
                (thread_id,),
            ).fetchall()
        return [row[0] for row in rows]

    async def list_versions(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> list[dict[str, Any]]:
        """列出所有版本（按 created_at 升序）。"""
        def _op() -> list[dict[str, Any]]:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT version_id, timestamp, metadata FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ?
                    ORDER BY created_at ASC""",
                    (thread_id, checkpoint_ns),
                ).fetchall()
            return [
                {"version_id": r[0], "timestamp": r[1], "metadata": json.loads(r[2]) if r[2] else {}}
                for r in rows
            ]

        try:
            return await _run_in_thread(_op)
        except Exception as exc:
            raise CheckpointError(
                f"列出版本失败（thread={thread_id!r} ns={checkpoint_ns!r}）：{exc}"
            ) from exc

    async def prune(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
        keep_latest: int = 10,
    ) -> int:
        """只保留最新 keep_latest 个版本，删除其余，返回删除数量。"""

        if keep_latest < 1:
            raise ValueError("keep_latest 必须大于等于 1")

        def _op() -> int:
            with self._connect() as conn:
                cursor = conn.execute(
                    """DELETE FROM checkpoints
                    WHERE thread_id = ? AND checkpoint_ns = ?
                    AND version_id NOT IN (
                        SELECT version_id FROM checkpoints
                        WHERE thread_id = ? AND checkpoint_ns = ?
                        ORDER BY created_at DESC LIMIT ?
                    )""",
                    (thread_id, checkpoint_ns, thread_id, checkpoint_ns, keep_latest),
                )
                return cursor.rowcount

        try:
            deleted = await _run_in_thread(_op)
        except Exception as exc:
            raise CheckpointError(
                f"清理 checkpoint 失败（thread={thread_id!r} ns={checkpoint_ns!r}）：{exc}"
            ) from exc

        if deleted > 0:
            logger.info("已清理 %d 个旧版本（thread=%r ns=%r）", deleted, thread_id, checkpoint_ns)
        return deleted

    @staticmethod
    def _row_to_state(row: tuple[Any, ...]) -> dict[str, Any]:
        """把一行 SQLite 记录转换为已迁移的 state 字典。"""

        keys = _LATEST_COLS.split(", ")
        value = dict(zip(keys, row, strict=True))
        state = json.loads(value["state"])
        # Schema 迁移：没有版本标记的旧状态从 v0 开始
        saved_version = int(value.get("schema_version", 0))
        if saved_version < CHECKPOINT_SCHEMA_VERSION:
            state = migrate_state(state, saved_version)
        return state
