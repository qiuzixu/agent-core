"""向量存储适配器和工厂的契约测试。"""

from __future__ import annotations

import importlib.util
import math
import os
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from typing import Any

from agent_core import (
    AccessContext,
    ChromaVectorStore,
    Document,
    InMemoryVectorStore,
    PgVectorStore,
    VectorQuery,
    VectorRecord,
    create_vector_store,
)


def _matches_where(metadata: Mapping[str, object], where: Mapping[str, object]) -> bool:
    if "$and" in where:
        clauses = where["$and"]
        return isinstance(clauses, Sequence) and all(
            isinstance(item, Mapping) and _matches_where(metadata, item) for item in clauses
        )
    if "$or" in where:
        clauses = where["$or"]
        return isinstance(clauses, Sequence) and any(
            isinstance(item, Mapping) and _matches_where(metadata, item) for item in clauses
        )
    for key, condition in where.items():
        if not isinstance(condition, Mapping) or "$eq" not in condition:
            return False
        if metadata.get(key) != condition["$eq"]:
            return False
    return True


class _FakeCollection:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def upsert(
        self,
        *,
        ids: Sequence[str],
        embeddings: Sequence[Sequence[float]],
        documents: Sequence[str],
        metadatas: Sequence[Mapping[str, object]],
    ) -> None:
        for record_id, embedding, document, metadata in zip(
            ids,
            embeddings,
            documents,
            metadatas,
            strict=True,
        ):
            self.records[record_id] = {
                "embedding": list(embedding),
                "document": document,
                "metadata": dict(metadata),
            }

    def get(self, *, ids: Sequence[str], include: Sequence[str]) -> dict[str, object]:
        del include
        existing = [record_id for record_id in ids if record_id in self.records]
        return {
            "ids": existing,
            "metadatas": [self.records[record_id]["metadata"] for record_id in existing],
        }

    def delete(self, *, ids: Sequence[str]) -> None:
        for record_id in ids:
            self.records.pop(record_id, None)

    def count(self) -> int:
        return len(self.records)

    def query(
        self,
        *,
        query_embeddings: Sequence[Sequence[float]],
        n_results: int,
        where: Mapping[str, object],
        include: Sequence[str],
    ) -> dict[str, object]:
        del include
        query = query_embeddings[0]
        matches: list[tuple[float, str, dict[str, Any]]] = []
        for record_id, record in self.records.items():
            metadata = record["metadata"]
            if not isinstance(metadata, Mapping) or not _matches_where(metadata, where):
                continue
            embedding = record["embedding"]
            assert isinstance(embedding, list)
            dot = sum(float(left) * float(right) for left, right in zip(query, embedding, strict=True))
            query_norm = math.sqrt(sum(float(value) ** 2 for value in query))
            record_norm = math.sqrt(sum(float(value) ** 2 for value in embedding))
            similarity = dot / (query_norm * record_norm)
            matches.append((1.0 - similarity, record_id, record))
        matches.sort(key=lambda item: (item[0], item[1]))
        selected = matches[:n_results]
        return {
            "ids": [[record_id for _, record_id, _ in selected]],
            "metadatas": [[record["metadata"] for _, _, record in selected]],
            "distances": [[distance for distance, _, _ in selected]],
            "embeddings": [[record["embedding"] for _, _, record in selected]],
        }


class _FakeChromaClient:
    def __init__(self) -> None:
        self.collections: dict[str, _FakeCollection] = {}

    def get_or_create_collection(self, *, name: str, metadata: Mapping[str, object]) -> _FakeCollection:
        del metadata
        return self.collections.setdefault(name, _FakeCollection())


class VectorStoreAdapterTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        os.getenv("AGENT_CORE_TEST_PGVECTOR_DSN"),
        "需要配置 AGENT_CORE_TEST_PGVECTOR_DSN",
    )
    async def test_real_pgvector_round_trip(self) -> None:
        dsn = os.environ["AGENT_CORE_TEST_PGVECTOR_DSN"]
        access = AccessContext(user_id="alice", tenant_id="tenant-a")
        store = PgVectorStore(
            dsn,
            dimension=2,
            collection_name="agent_core_integration_test",
            table_name="agent_vector_integration_test",
            require_access=True,
        )
        await store.initialize()
        try:
            await store.upsert(
                [
                    VectorRecord(
                        "real-pgvector-1",
                        (1.0, 0.0),
                        Document(content="真实 pgvector 文档", metadata={"category": "rule"}),
                        namespace="manual",
                    )
                ],
                access=access,
            )
            results = await store.search(
                VectorQuery(
                    vector=(1.0, 0.0),
                    namespace="manual",
                    metadata_filter={"category": "rule"},
                    access=access,
                )
            )
            self.assertEqual(results[0].record.record_id, "real-pgvector-1")
            self.assertEqual(await store.delete(["real-pgvector-1"], access=access), 1)
        finally:
            await store.close()

    @unittest.skipUnless(importlib.util.find_spec("chromadb"), "需要安装 chroma extra")
    async def test_real_chroma_persistent_client_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            access = AccessContext(user_id="alice", tenant_id="tenant-a")
            store = ChromaVectorStore(
                directory,
                collection_name="agent_core_test",
                require_access=True,
            )
            try:
                await store.upsert(
                    [
                        VectorRecord(
                            "real-chroma-1",
                            (1.0, 0.0),
                            Document(content="真实 Chroma 文档", metadata={"category": "rule"}),
                            namespace="manual",
                        )
                    ],
                    access=access,
                )
            finally:
                await store.close()

            reopened = ChromaVectorStore(
                directory,
                collection_name="agent_core_test",
                require_access=True,
            )
            try:
                results = await reopened.search(
                    VectorQuery(
                        vector=(1.0, 0.0),
                        namespace="manual",
                        metadata_filter={"category": "rule"},
                        access=access,
                    )
                )

                self.assertEqual(results[0].record.record_id, "real-chroma-1")
                self.assertEqual(results[0].record.document.content, "真实 Chroma 文档")
            finally:
                await reopened.close()

    async def test_chroma_persists_filters_and_enforces_access(self) -> None:
        client = _FakeChromaClient()
        alice = AccessContext(user_id="alice", tenant_id="tenant-a")
        bob = AccessContext(user_id="bob", tenant_id="tenant-a")
        store = ChromaVectorStore(client=client, require_access=True)
        record = VectorRecord(
            record_id="rule-1",
            vector=(1.0, 0.0),
            document=Document(content="机场净空规则", metadata={"category": "rule"}),
            namespace="manual",
            metadata={"version": 1},
        )

        self.assertEqual(await store.upsert([record], access=alice), ["rule-1"])
        reopened = ChromaVectorStore(client=client, require_access=True)
        results = await reopened.search(
            VectorQuery(
                vector=(1.0, 0.0),
                namespace="manual",
                metadata_filter={"category": "rule", "version": 1},
                access=alice,
            )
        )
        hidden = await reopened.search(VectorQuery(vector=(1.0, 0.0), namespace="manual", access=bob))

        self.assertEqual(results[0].record.document.content, "机场净空规则")
        self.assertAlmostEqual(results[0].score, 1.0)
        self.assertEqual(hidden, [])
        with self.assertRaises(PermissionError):
            await reopened.delete(["rule-1"], access=bob)
        self.assertEqual(await reopened.delete(["rule-1"], access=alice), 1)

    async def test_upsert_cannot_replace_another_users_record(self) -> None:
        alice = AccessContext(user_id="alice", tenant_id="tenant-a")
        bob = AccessContext(user_id="bob", tenant_id="tenant-a")
        store = InMemoryVectorStore(require_access=True)
        original = VectorRecord("shared-id", (1.0,), Document(content="Alice 文档"))
        replacement = VectorRecord("shared-id", (1.0,), Document(content="Bob 文档"))

        await store.upsert([original], access=alice)
        with self.assertRaises(PermissionError):
            await store.upsert([replacement], access=bob)
        with self.assertRaisesRegex(ValueError, "有限数值"):
            await store.upsert(
                [VectorRecord("invalid", (float("nan"),), Document(content="无效向量"))],
                access=alice,
            )

    async def test_factory_uses_environment_defaults_and_allows_override(self) -> None:
        self.assertIsInstance(create_vector_store("testing"), InMemoryVectorStore)
        client = _FakeChromaClient()
        self.assertIsInstance(
            create_vector_store("development", chroma_client=client),
            ChromaVectorStore,
        )
        postgres = create_vector_store(
            "production",
            postgres_url="postgresql://localhost/agent",
            dimension=1536,
        )
        self.assertIsInstance(postgres, PgVectorStore)
        self.assertIsInstance(
            create_vector_store("production", backend="memory"),
            InMemoryVectorStore,
        )

        with self.assertRaisesRegex(ValueError, "PostgreSQL"):
            create_vector_store("production", dimension=1536)
        with self.assertRaisesRegex(ValueError, "dimension"):
            create_vector_store("production", postgres_url="postgresql://localhost/agent")


if __name__ == "__main__":
    unittest.main()
