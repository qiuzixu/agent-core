"""FAISS 本地向量存储适配器测试；未安装 faiss-cpu 时自动跳过。"""

import importlib.util
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_core.access import AccessContext
from agent_core.documents import Document
from agent_core.retrieval.types import VectorQuery, VectorRecord

faiss_available = importlib.util.find_spec("faiss") is not None
numpy_available = importlib.util.find_spec("numpy") is not None
requires_faiss = pytest.mark.skipif(
    not (faiss_available and numpy_available), reason="需要安装 zero-agent-core[faiss]"
)


@pytest.fixture
def faiss_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def make_record(
    record_id: str,
    *,
    namespace: str = "default",
    user_id: str | None = None,
    tenant_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> VectorRecord:
    """构造固定维度的测试向量。"""
    vectors = {
        "1": (1.0, 0.0, 0.0),
        "2": (0.0, 1.0, 0.0),
        "3": (0.9, 0.1, 0.0),
    }
    return VectorRecord(
        record_id=record_id,
        vector=vectors[record_id],
        document=Document(content=f"content-{record_id}", metadata={"source": record_id}),
        namespace=namespace,
        user_id=user_id,
        tenant_id=tenant_id,
        metadata=dict(metadata or {}),
    )


@requires_faiss
async def test_faiss_round_trip_and_filters(faiss_dir: Path) -> None:
    """写入后可检索、namespace/metadata/权限过滤生效。"""
    from agent_core.retrieval import FaissVectorStore

    store = FaissVectorStore(faiss_dir, collection_name="test")
    await store.initialize()
    saved = await store.upsert([make_record("1"), make_record("2"), make_record("3")])
    assert saved == ["1", "2", "3"]

    results = await store.search(VectorQuery(vector=(1.0, 0.0, 0.0), limit=2))
    assert [item.record.record_id for item in results] == ["1", "3"]

    filtered = await store.search(
        VectorQuery(
            vector=(1.0, 0.0, 0.0),
            limit=2,
            metadata_filter={"source": "3"},
        )
    )
    assert [item.record.record_id for item in filtered] == ["3"]

    await store.close()


@requires_faiss
async def test_faiss_delete_and_persist_round_trip(faiss_dir: Path) -> None:
    """删除生效，并且同一目录重新打开后可以继续检索。"""
    from agent_core.retrieval import FaissVectorStore

    store = FaissVectorStore(faiss_dir, collection_name="persist")
    await store.initialize()
    await store.upsert([make_record("1"), make_record("2")])
    deleted = await store.delete(["1"])
    assert deleted == 1
    await store.close()

    reopened = FaissVectorStore(faiss_dir, collection_name="persist")
    await reopened.initialize()
    results = await reopened.search(VectorQuery(vector=(1.0, 0.0, 0.0), limit=2))
    assert [item.record.record_id for item in results] == ["2"]
    await reopened.close()


@requires_faiss
async def test_faiss_access_context_isolation(faiss_dir: Path) -> None:
    """开启访问控制后，跨用户查询必须被拒绝。"""
    from agent_core.retrieval import FaissVectorStore

    store = FaissVectorStore(faiss_dir, collection_name="access", require_access=True)
    await store.initialize()
    await store.upsert(
        [make_record("1", user_id="alice", tenant_id="tenant-a")],
        access=AccessContext(user_id="alice", tenant_id="tenant-a"),
    )

    results = await store.search(
        VectorQuery(
            vector=(1.0, 0.0, 0.0),
            limit=2,
            access=AccessContext(user_id="alice", tenant_id="tenant-a"),
        )
    )
    assert [item.record.record_id for item in results] == ["1"]

    with pytest.raises(PermissionError):
        await store.search(
            VectorQuery(
                vector=(1.0, 0.0, 0.0),
                limit=2,
                access=AccessContext(user_id="bob", tenant_id="tenant-a"),
            )
        )
    await store.close()
