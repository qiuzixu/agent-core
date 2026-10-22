"""Checkpointer 工厂函数：按环境自动选择后端。

选择规则与其余存储模块（session_event / idempotency 等）保持一致：
- test/testing     -> MemoryCheckpointer（快、随进程消失）
- development/*    -> SqliteCheckpointer（落盘、单实例够用）
- production/prod  -> PostgresCheckpointer（多实例，必须提供 DSN）

Original notes: select backend by environment.

选择模式与 create_session_event_store / create_workflow_idempotency_store 保持一致：
- test/testing     -> MemoryCheckpointer（快、随进程消失）
- development/*    -> SqliteCheckpointer（持久化、单实例）
- production/prod  -> PostgresCheckpointer（多实例、必须提供 DSN）

用法：

    from agent_core.checkpoint import create_checkpointer

    # 按环境自动选择：
    cp = create_checkpointer("development", sqlite_path="./data/checkpoints.db")
    cp = create_checkpointer("production", postgres_url="postgresql://...")

    # 强制指定后端：
    cp = create_checkpointer("development", backend="memory")
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from agent_core.checkpoint.store import Checkpointer, MemoryCheckpointer

__all__ = ["CheckpointerBackend", "create_checkpointer"]

CheckpointerBackend = Literal["memory", "sqlite", "postgres", "auto"]


def create_checkpointer(
    env: str = "development",
    *,
    backend: CheckpointerBackend | None = None,
    sqlite_path: str | Path = "./checkpoints.db",
    postgres_url: str | None = None,
) -> Checkpointer:
    """根据环境或显式 backend 参数创建 Checkpointer 实例。

    Args:
        env: 环境名（"test" / "development" / "production"）。
        backend: 强制指定后端，优先于 env 自动选择。
            "auto" 或 None 表示按 env 自动选。
        sqlite_path: SQLite 数据库文件路径（开发环境默认）。
        postgres_url: PostgreSQL DSN（生产环境必填）。

    Returns:
        Checkpointer 实例。

    Raises:
        ValueError: 生产环境未提供 postgres_url 时抛出。
    """
    # 显式指定 backend 时优先于环境自动选择
    if backend and backend != "auto":
        return _create_by_backend(backend, sqlite_path, postgres_url)

    # 按环境自动选择
    normalized = env.lower().strip()
    if normalized in {"test", "testing"}:
        return MemoryCheckpointer()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("production 环境必须提供 postgres_url 才能创建 checkpointer")
        return _create_by_backend("postgres", sqlite_path, postgres_url)
    return _create_by_backend("sqlite", sqlite_path, postgres_url)


def _create_by_backend(
    backend: str,
    sqlite_path: str | Path,
    postgres_url: str | None,
) -> Checkpointer:
    """按 backend 名称实例化对应的 Checkpointer。"""

    if backend == "memory":
        return MemoryCheckpointer()
    if backend == "sqlite":
        from agent_core.checkpoint.sqlite import SqliteCheckpointer
        return SqliteCheckpointer(sqlite_path)
    if backend == "postgres":
        if not postgres_url:
            raise ValueError("显式指定 postgres 后端时必须提供 postgres_url")
        from agent_core.checkpoint.postgres import PostgresCheckpointer
        return PostgresCheckpointer(postgres_url)
    raise ValueError(f"Unknown checkpointer backend: {backend!r}. Use 'memory', 'sqlite', or 'postgres'.")
