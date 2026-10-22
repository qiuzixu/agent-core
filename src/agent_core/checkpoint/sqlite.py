"""SQLite checkpointer backend.

Production-ready checkpoint storage using SQLite (WAL mode).
Supports save / load / delete / list_namespaces / list_versions per
(thread_id, checkpoint_ns). Uses the same sqlite_connection utils
as other storage modules for consistent timeout and WAL behavior.
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

_LATEST_COLS = """thread_id, checkpoint_ns, version_id, timestamp, state, metadata, branch, schema_version"""


class SqliteCheckpointer:
    """SQLite (WAL) checkpointer with version history.

    Every save() creates a new version row. load() without version_id
    returns the latest version's state (with schema migration applied).
    list_namespaces / list_versions provide trace_tree support.
    """

    def __init__(self, db_path: str | Path = "./checkpoints.db") -> None:
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
                f"Failed to save checkpoint for thread {thread_id!r} ns {checkpoint_ns!r}: {exc}"
            ) from exc

        logger.debug(
            "Saved checkpoint %s for thread %r ns %r (schema v%d)",
            version_id, thread_id, checkpoint_ns, schema_version,
        )
        return version_id

    async def load(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> dict[str, Any] | None:
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
                f"Failed to load checkpoint for thread {thread_id!r} ns {checkpoint_ns!r}: {exc}"
            ) from exc

    async def delete(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
    ) -> None:
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
                f"Failed to delete checkpoint for thread {thread_id!r} ns {checkpoint_ns!r}: {exc}"
            ) from exc

    def list_threads(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id").fetchall()
        return [row[0] for row in rows]

    def list_namespaces(self, thread_id: str) -> list[str]:
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
                f"Failed to list versions for thread {thread_id!r} ns {checkpoint_ns!r}: {exc}"
            ) from exc

    async def prune(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = DEFAULT_CHECKPOINT_NS,
        keep_latest: int = 10,
    ) -> int:
        """Delete all but the most recent `keep_latest` versions. Returns deleted count."""
        if keep_latest < 1:
            raise ValueError("keep_latest must be >= 1")

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
                f"Failed to prune checkpoints for thread {thread_id!r} ns {checkpoint_ns!r}: {exc}"
            ) from exc

        if deleted > 0:
            logger.info("Pruned %d old checkpoint versions for thread %r ns %r", deleted, thread_id, checkpoint_ns)
        return deleted

    @staticmethod
    def _row_to_state(row: tuple[Any, ...]) -> dict[str, Any]:
        """Convert a SQLite row to a migrated state dict."""
        keys = _LATEST_COLS.split(", ")
        value = dict(zip(keys, row, strict=True))
        state = json.loads(value["state"])
        # Schema migration: old states without version stamp start at v0
        saved_version = int(value.get("schema_version", 0))
        if saved_version < CHECKPOINT_SCHEMA_VERSION:
            state = migrate_state(state, saved_version)
        return state
