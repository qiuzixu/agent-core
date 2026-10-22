"""SQLite Checkpointer tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_core.checkpoint.schema import CHECKPOINT_SCHEMA_VERSION
from agent_core.checkpoint.sqlite import SqliteCheckpointer
from agent_core.checkpoint.trace import trace_tree


class TestSqliteCheckpointer:
    """SqliteCheckpointer basic operations."""

    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            vid = await sc.save("t1", {"a": 1, "b": "hello"})
            assert vid is not None

            loaded = await sc.load("t1")
            assert loaded is not None
            assert loaded["a"] == 1
            assert loaded["b"] == "hello"
            assert loaded["_checkpoint_metadata"]["schema_version"] == CHECKPOINT_SCHEMA_VERSION

        asyncio.run(run())

    def test_save_creates_new_versions(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            await sc.save("t1", {"step": 1})
            await sc.save("t1", {"step": 2})
            await sc.save("t1", {"step": 3})

            versions = await sc.list_versions("t1")
            assert len(versions) == 3

            # Latest version wins
            loaded = await sc.load("t1")
            assert loaded["step"] == 3

        asyncio.run(run())

    def test_ns_isolation(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            await sc.save("t1", {"layer": "chat"})
            await sc.save("t1", {"layer": "workflow"}, checkpoint_ns="workflow:exec-1")

            chat = await sc.load("t1")
            wf = await sc.load("t1", checkpoint_ns="workflow:exec-1")
            assert chat["layer"] == "chat"
            assert wf["layer"] == "workflow"

        asyncio.run(run())

    def test_list_namespaces(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            await sc.save("t1", {"a": 1})
            await sc.save("t1", {"b": 2}, checkpoint_ns="workflow:x")
            await sc.save("t1", {"c": 3}, checkpoint_ns="workflow:y")
            await sc.save("t2", {"d": 4})

            ns = sc.list_namespaces("t1")
            assert ns == ["", "workflow:x", "workflow:y"]
            assert sc.list_namespaces("t2") == [""]

        asyncio.run(run())

    def test_delete(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            await sc.save("t1", {"a": 1})
            assert await sc.load("t1") is not None
            await sc.delete("t1")
            assert await sc.load("t1") is None

        asyncio.run(run())

    def test_delete_ns_isolated(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            await sc.save("t1", {"a": 1})
            await sc.save("t1", {"b": 2}, checkpoint_ns="workflow:x")
            await sc.delete("t1", checkpoint_ns="workflow:x")

            assert await sc.load("t1") is not None
            assert await sc.load("t1", checkpoint_ns="workflow:x") is None

        asyncio.run(run())

    def test_prune(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            for i in range(10):
                await sc.save("t1", {"step": i})

            deleted = await sc.prune("t1", keep_latest=3)
            assert deleted == 7

            versions = await sc.list_versions("t1")
            assert len(versions) == 3
            loaded = await sc.load("t1")
            assert loaded["step"] == 9  # latest preserved

        asyncio.run(run())

    def test_prune_keep_all(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            for i in range(5):
                await sc.save("t1", {"step": i})

            deleted = await sc.prune("t1", keep_latest=10)
            assert deleted == 0

        asyncio.run(run())

    def test_trace_tree(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            await sc.save("t1", {"a": 1})
            await sc.save("t1", {"b": 2}, checkpoint_ns="workflow:exec-1")

            tree = await trace_tree(sc, "t1")
            assert tree["thread_id"] == "t1"
            assert "" in tree["namespaces"]
            assert "workflow:exec-1" in tree["namespaces"]
            assert tree["namespaces"][""]["versions"] == 1
            assert tree["namespaces"]["workflow:exec-1"]["versions"] == 1

        asyncio.run(run())

    def test_list_threads(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            await sc.save("t1", {"a": 1})
            await sc.save("t2", {"b": 2})
            threads = sc.list_threads()
            assert threads == ["t1", "t2"]

        asyncio.run(run())

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        async def run() -> None:
            sc = SqliteCheckpointer(tmp_path / "test.db")
            loaded = await sc.load("nonexistent")
            assert loaded is None

        asyncio.run(run())
