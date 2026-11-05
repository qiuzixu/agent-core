"""事件 Outbox 与工具操作去重的默认存储实现。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_core.protocol.runtime import RunEvent, ToolResult
from agent_core.storage._sqlite_utils import _run_in_thread
from agent_core.storage._sqlite_utils import ensure_wal as _ensure_wal


def _clone_event(event: RunEvent) -> RunEvent:
    """复制事件，避免调用方继续修改待投递对象。"""
    return RunEvent.from_dict(event.to_dict())


def _clone_result(result: ToolResult) -> ToolResult:
    """复制工具结果，避免缓存对象被调用方修改。"""
    return ToolResult.from_dict(result.to_dict())


class MemoryEventOutbox:
    """进程内事件 Outbox，适合测试和单进程开发。"""

    def __init__(self) -> None:
        self._events: dict[str, tuple[RunEvent, bool]] = {}

    async def append(self, event: RunEvent) -> None:
        self._events.setdefault(event.event_id, (_clone_event(event), False))

    async def pending(self, run_id: str, *, limit: int = 100) -> list[RunEvent]:
        values = [
            event
            for event, published in self._events.values()
            if event.run_id == run_id and not published
        ]
        values.sort(key=lambda item: item.sequence)
        return [_clone_event(event) for event in values[: max(1, int(limit))]]

    async def mark_published(self, event_id: str) -> None:
        current = self._events.get(event_id)
        if current is not None:
            self._events[event_id] = (current[0], True)


class MemoryToolOperationStore:
    """进程内非幂等工具结果存储。"""

    def __init__(self) -> None:
        self._results: dict[str, tuple[str, str, ToolResult]] = {}
        self._inflight: set[str] = set()
        self._lock = asyncio.Lock()

    async def claim(
        self,
        operation_id: str,
        *,
        tool_name: str,
        arguments_hash: str,
    ) -> tuple[bool, ToolResult | None]:
        async with self._lock:
            current = self._results.get(operation_id)
            if current is not None:
                if (current[0], current[1]) != (tool_name, arguments_hash):
                    raise ValueError(f"operation_id {operation_id!r} 已绑定其他工具参数")
                return False, _clone_result(current[2])
            if operation_id in self._inflight:
                return False, None
            self._inflight.add(operation_id)
            return True, None

    async def get(
        self,
        operation_id: str,
        *,
        tool_name: str,
        arguments_hash: str,
    ) -> ToolResult | None:
        async with self._lock:
            current = self._results.get(operation_id)
            if current is None:
                return None
            stored_tool, stored_hash, result = current
            if (stored_tool, stored_hash) != (tool_name, arguments_hash):
                raise ValueError(f"operation_id {operation_id!r} 已绑定其他工具参数")
            return _clone_result(result)

    async def save(
        self,
        operation_id: str,
        result: ToolResult,
        *,
        tool_name: str,
        arguments_hash: str,
    ) -> None:
        async with self._lock:
            current = self._results.get(operation_id)
            if current is not None and (current[0], current[1]) != (tool_name, arguments_hash):
                raise ValueError(f"operation_id {operation_id!r} 已绑定其他工具参数")
            self._results.setdefault(
                operation_id,
                (tool_name, arguments_hash, _clone_result(result)),
            )
            self._inflight.discard(operation_id)

    async def release(self, operation_id: str) -> None:
        async with self._lock:
            self._inflight.discard(operation_id)


class SqliteEventOutbox:
    """SQLite 事件 Outbox，进程重启后仍可继续投递未确认事件。"""

    DDL = """
    CREATE TABLE IF NOT EXISTS agent_event_outbox (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        event_json TEXT NOT NULL,
        published INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_agent_event_outbox_pending
        ON agent_event_outbox(run_id, published, sequence);
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(Path(db_path).resolve())
        with sqlite3.connect(self._db_path, timeout=30.0) as conn:
            _ensure_wal(conn)
            conn.executescript(self.DDL)

    async def append(self, event: RunEvent) -> None:
        value = json.dumps(event.to_dict(), ensure_ascii=False)
        created_at = datetime.now(UTC).isoformat()

        def op() -> None:
            with sqlite3.connect(self._db_path, timeout=30.0) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO agent_event_outbox"
                    "(event_id, run_id, sequence, event_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (event.event_id, event.run_id, event.sequence, value, created_at),
                )

        await _run_in_thread(op)

    async def pending(self, run_id: str, *, limit: int = 100) -> list[RunEvent]:
        def op() -> list[tuple[Any, ...]]:
            with sqlite3.connect(self._db_path, timeout=30.0) as conn:
                return conn.execute(
                    "SELECT event_json FROM agent_event_outbox "
                    "WHERE run_id=? AND published=0 ORDER BY sequence LIMIT ?",
                    (run_id, max(1, int(limit))),
                ).fetchall()

        rows = await _run_in_thread(op)
        return [RunEvent.from_dict(json.loads(str(row[0]))) for row in rows]

    async def mark_published(self, event_id: str) -> None:
        def op() -> None:
            with sqlite3.connect(self._db_path, timeout=30.0) as conn:
                conn.execute(
                    "UPDATE agent_event_outbox SET published=1 WHERE event_id=?",
                    (event_id,),
                )

        await _run_in_thread(op)


class SqliteToolOperationStore:
    """SQLite 非幂等工具结果存储，确保重启恢复时不会重复提交副作用。"""

    DDL = """
    CREATE TABLE IF NOT EXISTS agent_tool_operations (
        operation_id TEXT PRIMARY KEY,
        tool_name TEXT NOT NULL,
        arguments_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        result_json TEXT,
        created_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(Path(db_path).resolve())
        with sqlite3.connect(self._db_path, timeout=30.0) as conn:
            _ensure_wal(conn)
            conn.executescript(self.DDL)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(agent_tool_operations)")}
            if "status" not in columns:
                conn.execute(
                    "ALTER TABLE agent_tool_operations ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
                )

    async def get(
        self,
        operation_id: str,
        *,
        tool_name: str,
        arguments_hash: str,
    ) -> ToolResult | None:
        def op() -> Any:
            with sqlite3.connect(self._db_path, timeout=30.0) as conn:
                return conn.execute(
                    "SELECT tool_name, arguments_hash, status, result_json "
                    "FROM agent_tool_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()

        row = await _run_in_thread(op)
        if row is None:
            return None
        if (str(row[0]), str(row[1])) != (tool_name, arguments_hash):
            raise ValueError(f"operation_id {operation_id!r} 已绑定其他工具参数")
        if str(row[2]) != "completed" or row[3] is None:
            return None
        return ToolResult.from_dict(json.loads(str(row[3])))

    async def claim(
        self,
        operation_id: str,
        *,
        tool_name: str,
        arguments_hash: str,
    ) -> tuple[bool, ToolResult | None]:
        """用 SQLite 写锁原子认领操作，跨进程只允许一个执行者。"""
        def op() -> tuple[bool, str | None]:
            with sqlite3.connect(self._db_path, timeout=30.0) as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT tool_name, arguments_hash, status, result_json "
                    "FROM agent_tool_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is not None:
                    if (str(row[0]), str(row[1])) != (tool_name, arguments_hash):
                        raise ValueError(f"operation_id {operation_id!r} 已绑定其他工具参数")
                    conn.commit()
                    return False, str(row[3]) if str(row[2]) == "completed" and row[3] else None
                conn.execute(
                    "INSERT INTO agent_tool_operations"
                    "(operation_id, tool_name, arguments_hash, status, result_json, created_at) "
                    "VALUES (?, ?, ?, 'running', '{}', ?)",
                    (operation_id, tool_name, arguments_hash, datetime.now(UTC).isoformat()),
                )
                conn.commit()
                return True, None

        owner, result_json = await _run_in_thread(op)
        return owner, ToolResult.from_dict(json.loads(result_json)) if result_json else None

    async def save(
        self,
        operation_id: str,
        result: ToolResult,
        *,
        tool_name: str,
        arguments_hash: str,
    ) -> None:
        value = json.dumps(result.to_dict(), ensure_ascii=False)
        created_at = datetime.now(UTC).isoformat()

        def op() -> None:
            with sqlite3.connect(self._db_path, timeout=30.0) as conn:
                current = conn.execute(
                    "SELECT tool_name, arguments_hash FROM agent_tool_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if current is not None and (str(current[0]), str(current[1])) != (tool_name, arguments_hash):
                    raise ValueError(f"operation_id {operation_id!r} 已绑定其他工具参数")
                conn.execute(
                    "UPDATE agent_tool_operations SET status='completed', result_json=? "
                    "WHERE operation_id=? AND status='running'",
                    (value, operation_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO agent_tool_operations"
                    "(operation_id, tool_name, arguments_hash, status, result_json, created_at) "
                    "VALUES (?, ?, ?, 'completed', ?, ?)",
                    (operation_id, tool_name, arguments_hash, value, created_at),
                )

        await _run_in_thread(op)

    async def release(self, operation_id: str) -> None:
        def op() -> None:
            with sqlite3.connect(self._db_path, timeout=30.0) as conn:
                conn.execute(
                    "DELETE FROM agent_tool_operations WHERE operation_id=? AND status='running'",
                    (operation_id,),
                )

        await _run_in_thread(op)


__all__ = [
    "MemoryEventOutbox",
    "MemoryToolOperationStore",
    "SqliteEventOutbox",
    "SqliteToolOperationStore",
]
