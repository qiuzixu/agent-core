"""Checkpointer factory tests."""

from __future__ import annotations

import pytest

from agent_core.checkpoint.factory import create_checkpointer
from agent_core.checkpoint.store import MemoryCheckpointer


class TestCreateCheckpointer:
    def test_test_env_returns_memory(self) -> None:
        cp = create_checkpointer("test")
        assert isinstance(cp, MemoryCheckpointer)

    def test_testing_env_returns_memory(self) -> None:
        cp = create_checkpointer("testing")
        assert isinstance(cp, MemoryCheckpointer)

    def test_dev_env_returns_sqlite(self, tmp_path) -> None:
        from agent_core.checkpoint.sqlite import SqliteCheckpointer
        cp = create_checkpointer("development", sqlite_path=tmp_path / "test.db")
        assert isinstance(cp, SqliteCheckpointer)

    def test_prod_without_dsn_raises(self) -> None:
        with pytest.raises(ValueError, match="postgres_url"):
            create_checkpointer("production")

    def test_explicit_backend_memory(self) -> None:
        cp = create_checkpointer("development", backend="memory")
        assert isinstance(cp, MemoryCheckpointer)

    def test_explicit_backend_sqlite(self, tmp_path) -> None:
        from agent_core.checkpoint.sqlite import SqliteCheckpointer
        cp = create_checkpointer("test", backend="sqlite", sqlite_path=tmp_path / "test.db")
        assert isinstance(cp, SqliteCheckpointer)

    def test_explicit_backend_postgres_no_dsn_raises(self) -> None:
        with pytest.raises(ValueError, match="postgres_url"):
            create_checkpointer("development", backend="postgres")

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown"):
            create_checkpointer("development", backend="redis")
