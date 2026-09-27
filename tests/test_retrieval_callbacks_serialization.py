"""Callback、文档检索和安全序列化的回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core import (
    AccessContext,
    AgentRuntime,
    BaseCallbackHandler,
    Blob,
    CallbackManager,
    Document,
    EmbeddingRetriever,
    InMemoryVectorStore,
    KeywordRetriever,
    MemoryRunStore,
    ReActAgent,
    RecursiveCharacterTextSplitter,
    RetrievalQuery,
    RunContext,
    SearchType,
    SerializedEnvelope,
    SerializerRegistry,
    SqliteRuntimeStore,
    StreamChunk,
    TextLoader,
    ToolExecutor,
    ToolRegistry,
    UnknownSerializedTypeError,
    assistant_message,
    dumps,
    loads,
)
from agent_core.protocol import RunEvent


class _Model:
    async def chat(self, _messages: Any, *, tools: Any = None) -> Any:
        del tools
        return assistant_message("完成")

    async def stream_chat(
        self,
        _messages: Any,
        *,
        tools: Any = None,
    ) -> AsyncIterator[StreamChunk]:
        del tools
        yield StreamChunk(text="完")
        yield StreamChunk(text="成")


class _Collector(BaseCallbackHandler):
    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def on_event(self, event: RunEvent) -> None:
        self.events.append(event)


class _BrokenCallback(BaseCallbackHandler):
    async def on_event(self, event: RunEvent) -> None:
        raise RuntimeError(f"broken:{event.event_type}")


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_dispatches_one_event_model_to_callbacks(self) -> None:
        collector = _Collector()
        manager = CallbackManager([_BrokenCallback(), collector])
        agent = ReActAgent(
            _Model(),
            ToolExecutor(ToolRegistry()),
            system_prompt="测试",
            tool_definitions=[],
        )
        runtime = AgentRuntime(
            agent,
            run_store=MemoryRunStore(),
            callback_manager=manager,
        )

        context = await runtime.run(
            "thread-callback",
            "开始",
            user_id="alice",
            tenant_id="tenant-a",
            parent_run_id="parent-run",
            tags=["test", "callback"],
        )

        self.assertEqual(context.status, "completed")
        self.assertEqual(
            [event.event_type for event in collector.events],
            ["run_started", "model_finished", "run_completed"],
        )
        self.assertTrue(all(event.parent_run_id == "parent-run" for event in collector.events))
        self.assertTrue(all(event.tags == ["test", "callback"] for event in collector.events))
        self.assertEqual(collector.events[1].component, "model")

    async def test_stream_callback_receives_incremental_chunks(self) -> None:
        collector = _Collector()
        agent = ReActAgent(
            _Model(),
            ToolExecutor(ToolRegistry()),
            system_prompt="测试",
            tool_definitions=[],
        )
        runtime = AgentRuntime(agent, callbacks=[collector])

        chunks = [chunk async for chunk in runtime.stream("thread-stream-callback", "开始")]

        self.assertEqual(chunks, ["完", "成"])
        deltas = [
            event.payload["delta"] for event in collector.events if event.event_type == "model_stream_chunk"
        ]
        self.assertEqual(deltas, chunks)

    async def test_sqlite_preserves_parent_tags_and_enriched_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteRuntimeStore(Path(directory) / "runtime.db")
            context = RunContext(
                thread_id="thread-sqlite-event",
                run_id="run-sqlite-event",
                parent_run_id="parent-run",
                tags=["audit"],
            )
            context.emit("retrieval_finished", component="retrieval", duration_ms=12.5, count=2)
            await store.save_run(context)

            loaded = await store.load_run(context.thread_id, context.run_id)

        assert loaded is not None
        self.assertEqual(loaded.parent_run_id, "parent-run")
        self.assertEqual(loaded.tags, ["audit"])
        self.assertEqual(loaded.events[0].component, "retrieval")
        self.assertEqual(loaded.events[0].duration_ms, 12.5)


class DocumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_loader_and_splitter_preserve_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge.txt"
            path.write_text("第一段内容。第二段内容。第三段内容。", encoding="utf-8")
            documents = await TextLoader(path, metadata={"namespace": "manual"}).load()
            reloaded = await TextLoader(path, metadata={"namespace": "manual"}).load()

        splitter = RecursiveCharacterTextSplitter(chunk_size=10, chunk_overlap=2)
        chunks = splitter.split_documents(documents)
        reloaded_chunks = splitter.split_documents(reloaded)

        self.assertGreater(len(chunks), 1)
        self.assertEqual(documents[0].document_id, reloaded[0].document_id)
        self.assertEqual(
            [chunk.document_id for chunk in chunks],
            [chunk.document_id for chunk in reloaded_chunks],
        )
        self.assertTrue(all(len(chunk.content) <= 10 for chunk in chunks))
        self.assertTrue(all(chunk.parent_document_id == documents[0].document_id for chunk in chunks))
        for chunk in chunks:
            assert chunk.locator is not None
            self.assertEqual(
                chunk.content,
                documents[0].content[chunk.locator.start_offset : chunk.locator.end_offset],
            )


class _Embeddings:
    @staticmethod
    def _embed(text: str) -> list[float]:
        return [float(text.count("机场")), float(text.count("无人机")), 1.0]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_retriever_enforces_access_and_namespace(self) -> None:
        store = InMemoryVectorStore(require_access=True)
        retriever = EmbeddingRetriever(_Embeddings(), store)
        alice = AccessContext(user_id="alice", tenant_id="tenant-a")
        bob = AccessContext(user_id="bob", tenant_id="tenant-a")
        airport = Document(content="机场运行规定", metadata={"kind": "rule"})
        drone = Document(content="无人机维修手册", metadata={"kind": "manual"})
        await retriever.add_documents([airport, drone], namespace="knowledge", access=alice)

        results = await retriever.retrieve(
            RetrievalQuery(
                "机场",
                namespace="knowledge",
                metadata_filter={"kind": "rule"},
                access=alice,
            )
        )
        hidden = await retriever.retrieve(RetrievalQuery("机场", namespace="knowledge", access=bob))

        self.assertEqual(results[0].document.document_id, airport.document_id)
        self.assertEqual(hidden, [])

    async def test_keyword_retriever_is_a_document_retriever(self) -> None:
        document = Document(
            content="低空航线需要经过审批",
            metadata={"namespace": "rules", "category": "flight"},
        )
        retriever = KeywordRetriever([document])

        results = await retriever.retrieve(
            RetrievalQuery(
                "审批",
                namespace="rules",
                search_type=SearchType.KEYWORD,
                metadata_filter={"category": "flight"},
            )
        )

        self.assertEqual(results[0].document.document_id, document.document_id)
        self.assertEqual(results[0].rank, 1)


class SerializationTests(unittest.TestCase):
    def test_default_registry_round_trips_blob_and_document(self) -> None:
        blob = Blob.from_text("中文文档", source="memory://demo")
        document = Document(content="可序列化文档", source="memory://document")

        loaded_blob = loads(dumps(blob))
        loaded_document = loads(dumps(document))

        self.assertEqual(loaded_blob.data, blob.data)
        self.assertEqual(loaded_blob.checksum, blob.checksum)
        self.assertEqual(loaded_document, document)

        context = RunContext(thread_id="thread-serialization", run_id="run-serialization")
        context.emit("run_started")
        loaded_context = loads(dumps(context))
        self.assertEqual(loaded_context.events[0].event_type, "run_started")

    def test_registry_applies_explicit_schema_migration(self) -> None:
        @dataclass(frozen=True)
        class Example:
            name: str
            enabled: bool

        registry = SerializerRegistry()
        registry.register(
            "test.example",
            Example,
            schema_version=2,
            encoder=lambda value: {"name": value.name, "enabled": value.enabled},
            decoder=lambda value: Example(str(value["name"]), bool(value["enabled"])),
            migrations={1: lambda value: {**value, "enabled": True}},
        )

        loaded = registry.load(SerializedEnvelope("test.example", 1, {"name": "legacy"}))

        self.assertEqual(loaded, Example("legacy", True))

    def test_unknown_type_cannot_trigger_dynamic_import(self) -> None:
        registry = SerializerRegistry()
        with self.assertRaises(UnknownSerializedTypeError):
            registry.load(
                {
                    "type": "os.system",
                    "schema_version": 1,
                    "payload": {"command": "ignored"},
                }
            )


if __name__ == "__main__":
    unittest.main()
