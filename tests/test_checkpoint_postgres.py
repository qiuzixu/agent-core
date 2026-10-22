"""PostgreSQL Checkpointer contract tests (skip if asyncpg not installed or no PG)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# Skip if asyncpg is not installed
asyncpg = pytest.importorskip("asyncpg")

DSN = os.environ.get("AGENT_CORE_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="AGENT_CORE_TEST_PG_DSN not set")

from agent_core.checkpoint.postgres import PostgresCheckpointer
from agent_core.checkpoint.schema import CHECKPOINT_SCHEMA_VERSION
from agent_core.checkpoint.trace import trace_tree


@pytest.fixture()
async def checkpointer() -> PostgresCheckpointer:
    cp = PostgresCheckpointer(DSN)
    await cp.initialize()
    # Clean test table
    async with cp._pool.acquire() as conn:
        await conn.execute("DELETE FROM checkpoints WHERE thread_id LIKE 'test-%'")
    yield cp
    await cp.close()


class TestPostgresCheckpointer:
    async def test_save_load_roundtrip(self, checkpointer: PostgresCheckpointer) -> None:
        vid = await checkpointer.save("test-t1", {"a": 1, "b": "hello"})
        assert vid is not None

        loaded = await checkpointer.load("test-t1")
        assert loaded is not None
        assert loaded["a"] == 1
        assert loaded["b"] == "hello"

    async def test_version_history(self, checkpointer: PostgresCheckpointer) -> None:
        for i in range(3):
            await checkpointer.save("test-t1", {"step": i})

        versions = await checkpointer.list_versions("test-t1")
        assert len(versions) == 3

        loaded = await checkpointer.load("test-t1")
        assert loaded["step"] == 2

    async def test_ns_isolation(self, checkpointer: PostgresCheckpointer) -> None:
        await checkpointer.save("test-t1", {"layer": "chat"})
        await checkpointer.save("test-t1", {"layer": "workflow"}, checkpoint_ns="workflow:exec-1")

        chat = await checkpointer.load("test-t1")
        wf = await checkpointer.load("test-t1", checkpoint_ns="workflow:exec-1")
        assert chat["layer"] == "chat"
        assert wf["layer"] == "workflow"

    async def test_delete(self, checkpointer: PostgresCheckpointer) -> None:
        await checkpointer.save("test-t1", {"a": 1})
        await checkpointer.delete("test-t1")
        assert await checkpointer.load("test-t1") is None

    async def test_prune(self, checkpointer: PostgresCheckpointer) -> None:
        for i in range(10):
            await checkpointer.save("test-t1", {"step": i})

        deleted = await checkpointer.prune("test-t1", keep_latest=3)
        assert deleted == 7

        versions = await checkpointer.list_versions("test-t1")
        assert len(versions) == 3
        loaded = await checkpointer.load("test-t1")
        assert loaded["step"] == 9

    async def test_trace_tree(self, checkpointer: PostgresCheckpointer) -> None:
        await checkpointer.save("test-t1", {"a": 1})
        await checkpointer.save("test-t1", {"b": 2}, checkpoint_ns="workflow:exec-1")

        tree = await trace_tree(checkpointer, "test-t1")
        assert "" in tree["namespaces"]
        assert "workflow:exec-1" in tree["namespaces"]

    async def test_list_namespaces(self, checkpointer: PostgresCheckpointer) -> None:
        await checkpointer.save("test-t1", {"a": 1})
        await checkpointer.save("test-t1", {"b": 2}, checkpoint_ns="workflow:x")
        ns = await checkpointer.list_namespaces("test-t1")
        assert ns == ["", "workflow:x"]
