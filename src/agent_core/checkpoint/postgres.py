"""PostgreSQL checkpoint 后端（生产环境默认）。

- 与 SqliteCheckpointer 接口逐一对齐：save / load / delete /
  list_
- asyncpg 连接池异步访问，多实例部署共享同一张 checkpoints 表；namespaces / list_versions / prune，切换后端不改业务代码；
- state / metadata 用 JSONB 存储，为未来按内容查询留余地；
- 可选依赖：需要 pip install agent-core[postgresql]；
  production 环境未提供 DSN 时工厂函数直接报错（诚实失败，不静默降级）。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from agent_core.checkpoint.schema import (
    CHECKPOINT_SCHEMA_VERSION,
    ensure_schema_version,
    get_schema_version,
    migrate_state,
)
from agent_core.checkpoint.store import DEFAULT_CHECKPOINT_NS
from agent_core.errors import CheckpointError

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    version_id TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    state JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    branch TEXT NOT NULL DEFAULT 'main',
    schema_version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, version_id)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_thread_ns
ON checkpoints (thread_id, checkpoint_ns, created_at DESC);
"""


class PostgresCheckpointer:
    """PostgreSQL (asyncpg) checkpointer with version history.

    Must call `await initialize()` before use.
    Every save() creates a new version row. load() without version_id
    returns the latest latest state (with schema migration applied).
    """

    def __init__(self, dsn: str) -> None:
        """保存 DSN 并初始化连接池为 None（需显式调 await initialize()）。"""
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any = None

    async def initialize(self) -> None:
        """创建连接池并执行建表 DDL。必须在其他操作前调用一次。"""

        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "PostgresCheckpointer 需要 asyncpg, 请安装: pip install agent-core[postgresql]"
            ) from exc
        # min=2：常驻连接避免请求冷启动；max=10：防止单实例占满 PG 连接上限。
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    async def close(self) -> None:
        """关闭连接池，释放资源。关闭后不可再使用。"""

        if self._pool:
            await self._pool.close()
            self._pool = None

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("PostgresCheckpointer 未初始化, 请先调用 await initialize()")

    async def save(
        self,
        thread_id: str,
        state: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> str:
        """保存新版本，返回版本 ID。"""

        self._check()
        version_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        stamped = ensure_schema_version(state)
        schema_version = get_schema_version(stamped)

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO checkpoints
                    (thread_id, checkpoint_ns, version_id, timestamp, state, metadata, branch, schema_version, created_at)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9)""",
                    thread_id,
                    checkpoint_ns,
                    version_id,
                    now,
                    json.dumps(stamped, ensure_ascii=False),
                    json.dumps(metadata or {}, ensure_ascii=False),
                    "main",
                    schema_version,
                    now,
                )
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
        """加载最新版本的 state（带 schema 自动迁移）。"""

        self._check()
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """SELECT state, schema_version FROM checkpoints
                    WHERE thread_id = $1 AND checkpoint_ns = $2
                    ORDER BY created_at DESC LIMIT 1""",
                    thread_id,
                    checkpoint_ns,
                )
        except Exception as exc:
            raise CheckpointError(
                f"加载 checkpoint 失败（thread={thread_id!r} ns={checkpoint_ns!r}）：{exc}"
            ) from exc

        if row is None:
            return None

        state = json.loads(row["state"])
        saved_version = int(row["schema_version"])
        if saved_version < CHECKPOINT_SCHEMA_VERSION:
            state = migrate_state(state, saved_version)

        # 剥离内部元数据, 对调用方透明
        result = dict(state)
        result.pop("_checkpoint_metadata", None)
        return result

    async def delete(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
        """删除该 (thread_id, ns) 下的全部版本."""
        self._check()
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = $1 AND checkpoint_ns = $2",
                    thread_id,
                    checkpoint_ns,
                )
        except Exception as exc:
            raise CheckpointError(
                f"删除 checkpoint 失败（thread={thread_id!r} ns={checkpoint_ns!r}）：{exc}"
            ) from exc

    async def list_threads(self) -> list[str]:
        """列出所有 thread_id (去重)."""
        self._check()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id")
        return [row["thread_id"] for row in rows]

    async def list_namespaces(self, thread_id: str) -> list[str]:
        """列出该 thread_id 下所有已使用的 checkpoint namespace."""
        self._check()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id = $1 ORDER BY checkpoint_ns",
                thread_id,
            )
        return [row["checkpoint_ns"] for row in rows]

    async def list_versions(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> list[dict[str, Any]]:
        """列出该 (thread_id, ns) 下所有版本 (按时间升序)."""
        self._check()
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT version_id, timestamp, metadata FROM checkpoints
                    WHERE thread_id = $1 AND checkpoint_ns = $2
                    ORDER BY created_at ASC""",
                    thread_id,
                    checkpoint_ns,
                )
        except Exception as exc:
            raise CheckpointError(
                f"列出版本失败（thread={thread_id!r} ns={checkpoint_ns!r}）：{exc}"
            ) from exc
        return [
            {
                "version_id": row["version_id"],
                "timestamp": row["timestamp"].isoformat() if hasattr(row["timestamp"], "isoformat") else str(row["timestamp"]),
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            }
            for row in rows
        ]

    async def prune(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
        keep_latest: int = 10,
    ) -> int:
        """删除最旧版本, 只保留 keep_latest 个最新版本.返回删除数量."""
        if keep_latest < 1:
            raise ValueError("keep_latest must be >= 1")
        self._check()
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    """DELETE FROM checkpoints
                    WHERE thread_id = $1 AND checkpoint_ns = $2
                    AND version_id NOT IN (
                        SELECT version_id FROM checkpoints
                        WHERE thread_id = $1 AND checkpoint_ns = $2
                        ORDER BY created_at DESC LIMIT $3
                    )""",
                    thread_id,
                    checkpoint_ns,
                    keep_latest,
                )
        except Exception as exc:
            raise CheckpointError(
                f"清理 checkpoint 失败（thread={thread_id!r} ns={checkpoint_ns!r}）：{exc}"
            ) from exc

        deleted = int(result.split()[-1]) if result else 0
        if deleted > 0:
            logger.info("Pruned %d old checkpoint versions for thread %r ns %r", deleted, thread_id, checkpoint_ns)
        return deleted
