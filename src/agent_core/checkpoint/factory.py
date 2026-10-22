"""Checkpointer factory: select backend by environment.

Follows the same pattern as create_session_event_store / create_workflow_idempotency_store:
- test/testing     -> MemoryCheckpointer (fast, ephemeral)
- development/*    -> SqliteCheckpointer (persistent, single-instance)
- production/prod  -> PostgresCheckpointer (multi-instance, requires DSN)

Usage:

    from agent_core.checkpoint import create_checkpointer

    # Automatic by env:
    cp = create_checkpointer("development", sqlite_path="./data/checkpoints.db")
    cp = create_checkpointer("production", postgres_url="postgresql://...")

    # Or force a specific backend:
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
    """Create a checkpointer instance based on environment or explicit backend.

    Args:
        env: Environment name ("test", "development", "production").
        backend: Force a specific backend (overrides env-based selection).
            "auto" or None means select by env.
        sqlite_path: SQLite database file path (development default).
        postgres_url: PostgreSQL DSN (required for production).

    Returns:
        A Checkpointer instance.

    Raises:
        ValueError: If production is selected without postgres_url.
    """
    # Explicit backend override
    if backend and backend != "auto":
        return _create_by_backend(backend, sqlite_path, postgres_url)

    # Auto-select by environment
    normalized = env.lower().strip()
    if normalized in {"test", "testing"}:
        return MemoryCheckpointer()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("Production environment requires postgres_url for checkpointer")
        return _create_by_backend("postgres", sqlite_path, postgres_url)
    return _create_by_backend("sqlite", sqlite_path, postgres_url)


def _create_by_backend(
    backend: str,
    sqlite_path: str | Path,
    postgres_url: str | None,
) -> Checkpointer:
    """Instantiate a checkpointer by backend name."""
    if backend == "memory":
        return MemoryCheckpointer()
    if backend == "sqlite":
        from agent_core.checkpoint.sqlite import SqliteCheckpointer
        return SqliteCheckpointer(sqlite_path)
    if backend == "postgres":
        if not postgres_url:
            raise ValueError("postgres_url is required for postgres backend")
        from agent_core.checkpoint.postgres import PostgresCheckpointer
        return PostgresCheckpointer(postgres_url)
    raise ValueError(f"Unknown checkpointer backend: {backend!r}. Use 'memory', 'sqlite', or 'postgres'.")
