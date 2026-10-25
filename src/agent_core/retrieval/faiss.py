"""FAISS 本地向量索引适配器。

FAISS 是高性能向量检索库，不是独立数据库服务；该实现把 FAISS 索引和
metadata JSON 保存到本地目录，适合单机开发或嵌入式部署。生产多实例
场景仍建议使用 pgvector 等共享数据库后端。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_core.access import AccessContext
from agent_core.retrieval._common import (
    check_record_access,
    encode_record_payload,
    metadata_matches,
    normalize_record,
    record_accessible,
    validate_vector,
)
from agent_core.retrieval.core import VectorStore
from agent_core.retrieval.types import VectorQuery, VectorRecord, VectorSearchResult

__all__ = ["FaissVectorStore"]


class FaissVectorStore(VectorStore):
    """基于 FAISS IndexIDMap2 的本地持久化向量存储。

    设计取舍：
    - FAISS 只负责向量索引，metadata / 文档 / 权限信息保存在 JSON sidecar；
    - 查询时先取候选，再在 Python 侧过滤 namespace、metadata 和访问权限；
    - 这是单机本地实现，跨进程并发写入需要调用方自己保证单写者。
    """

    def __init__(
        self,
        path: str | Path = "./faiss_db",
        *,
        collection_name: str = "agent_core",
        dimension: int | None = None,
        require_access: bool = False,
        index: Any | None = None,
    ) -> None:
        """构造本地 FAISS 存储；显式注入 index 主要用于测试。"""
        if not collection_name.strip():
            raise ValueError("collection_name 不能为空")
        if dimension is not None and dimension <= 0:
            raise ValueError("dimension 必须大于 0")
        if index is not None and dimension is None:
            dimension = int(index.d)

        self._root = Path(path).resolve()
        self._collection_name = collection_name
        self._dimension = dimension
        self._require_access = require_access
        self._external_index = index
        self._index: Any | None = index
        self._records: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

        # Windows / Linux 文件名合法字符差异较大，这里只保留通用安全字符。
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", collection_name).strip("._")
        if not safe_name:
            raise ValueError("collection_name 无法转换成合法文件名")
        self._index_file = self._root / f"{safe_name}.faiss"
        self._meta_file = self._root / f"{safe_name}.metadata.json"

    async def initialize(self) -> None:
        """创建目录、加载已有索引或延迟构造空索引。"""
        async with self._lock:
            if self._index is not None:
                return
            try:
                import faiss
            except ImportError as exc:
                raise ImportError(
                    "FaissVectorStore 需要 faiss-cpu，请安装 zero-agent-core[faiss]"
                ) from exc
            self._root.mkdir(parents=True, exist_ok=True)
            if self._index_file.exists():
                self._index = await asyncio.to_thread(faiss.read_index, str(self._index_file))
                assert self._index is not None  # faiss.read_index 成功时一定返回索引
                self._dimension = int(self._index.d)
                self._records = await asyncio.to_thread(self._read_metadata)
                self._next_id = max(self._records, default=0) + 1
                return

            if self._dimension is None:
                # 空库可以先不创建索引，等第一条向量到达时再确定维度。
                self._dimension = None
                self._index = None
                return
            self._index = self._create_index(int(self._dimension))
            self._persist_locked()

    async def upsert(
        self,
        records: Sequence[VectorRecord],
        *,
        access: AccessContext | None = None,
    ) -> list[str]:
        """写入或更新向量；重复 record_id 会先从 FAISS 索引中移除旧向量。"""
        if not records:
            return []
        normalized = [
            normalize_record(record, access, require_access=self._require_access)
            for record in records
        ]
        async with self._lock:
            self._ensure_index_for_vectors(tuple(record.vector for record in normalized))
            import faiss
            import numpy

            saved: list[str] = []
            for record in normalized:
                validate_vector(record.vector, dimension=self._dimension)
                existing = self._find_record_id(record.record_id)
                if existing is not None:
                    self._remove_ids([existing])
                faiss_id = self._next_id
                self._next_id += 1
                vector = self._as_matrix([record.vector])
                faiss.normalize_L2(vector)
                assert self._index is not None  # _ensure_index_for_vectors 已创建索引
                self._index.add_with_ids(vector, numpy.asarray([faiss_id], dtype="int64"))
                self._records[faiss_id] = self._serialize_record(record, vector=record.vector)
                saved.append(record.record_id)
            self._persist_locked()
            return saved

    async def delete(
        self,
        record_ids: Sequence[str],
        *,
        access: AccessContext | None = None,
    ) -> int:
        """按业务 record_id 删除向量；先完成权限校验再删除。"""
        if not record_ids:
            return 0
        async with self._lock:
            targets: list[int] = []
            for record_id in record_ids:
                faiss_id = self._find_record_id(record_id)
                if faiss_id is None:
                    continue
                self._check_record_access_locked(faiss_id, access)
                targets.append(faiss_id)
            if not targets:
                return 0
            self._remove_ids(targets)
            for faiss_id in targets:
                self._records.pop(faiss_id, None)
            self._persist_locked()
            return len(targets)

    async def search(self, query: VectorQuery) -> list[VectorSearchResult]:
        """执行余弦相似度检索，并在应用侧做 namespace / metadata / 权限过滤。"""
        self._check_required_access(query.access)
        async with self._lock:
            if self._index is None or self._index.ntotal == 0:
                return []
            import faiss

            # 本地 FAISS 不支持 metadata 索引，这里取全量候选后再过滤，
            # 保证 namespace / metadata / 权限过滤的结果是准确的。
            count = int(self._index.ntotal)
            vector = self._as_matrix([query.vector])
            validate_vector(query.vector, dimension=self._dimension)
            faiss.normalize_L2(vector)
            scores, ids = await asyncio.to_thread(
                self._index.search, vector, count
            )

            matches: list[VectorSearchResult] = []
            for raw_score, raw_id in zip(scores[0].tolist(), ids[0].tolist(), strict=True):
                if raw_id == -1 or raw_id not in self._records:
                    continue
                record = self._deserialize_record(raw_id)
                if record.namespace != query.namespace:
                    continue
                if not metadata_matches(record.metadata, query.metadata_filter):
                    continue
                if not record_accessible(record, query.access):
                    continue
                score = float(raw_score)
                if query.min_score is not None and score < query.min_score:
                    continue
                matches.append(VectorSearchResult(record=record, score=score))
            matches.sort(key=lambda item: (-item.score, item.record.record_id))
            return matches[: query.limit]

    async def close(self) -> None:
        """释放进程内索引对象；本地文件已经持久化。"""
        async with self._lock:
            self._index = None
            self._records.clear()

    def _ensure_index_for_vectors(self, vectors: Sequence[tuple[float, ...]]) -> None:
        """第一批写入时确定维度并创建 FAISS 索引。"""
        import faiss

        if self._dimension is None:
            if not vectors:
                raise ValueError("FAISS 空索引需要显式配置 dimension 或至少一条向量")
            self._dimension = len(vectors[0])
        if self._index is None:
            self._index = self._create_index(int(self._dimension))
        if self._external_index is None:
            self._root.mkdir(parents=True, exist_ok=True)

    def _create_index(self, dimension: int) -> Any:
        """创建带业务 ID 的精确检索索引；cosine 通过写入前归一化实现。"""
        import faiss

        return faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))

    def _as_matrix(self, vectors: Sequence[tuple[float, ...]]) -> Any:
        """把 tuple 向量转成 FAISS 需要的 float32 contiguous 矩阵。"""
        import numpy

        matrix = numpy.ascontiguousarray(
            [[float(value) for value in vector] for vector in vectors], dtype=numpy.float32
        )
        return matrix

    def _find_record_id(self, record_id: str) -> int | None:
        """按业务 record_id 反查 FAISS 内部 int64 ID。"""
        for faiss_id, item in self._records.items():
            if item.get("record_id") == record_id:
                return int(faiss_id)
        return None

    def _remove_ids(self, faiss_ids: Sequence[int]) -> None:
        """从 FAISS IndexIDMap2 中移除指定内部 ID。"""
        if self._index is None or not faiss_ids:
            return
        import faiss
        import numpy

        remove_ids = numpy.asarray(list(faiss_ids), dtype="int64")
        self._index.remove_ids(remove_ids)

    def _serialize_record(self, record: VectorRecord, *, vector: tuple[float, ...]) -> dict[str, Any]:
        """序列化 record；FAISS 只存向量，其余信息进 JSON sidecar。"""
        return {
            "record_id": record.record_id,
            "vector": list(vector),
            "namespace": record.namespace,
            "user_id": record.user_id,
            "tenant_id": record.tenant_id,
            "payload": encode_record_payload(record),
        }

    def _deserialize_record(self, faiss_id: int) -> VectorRecord:
        """从 JSON sidecar 还原完整 VectorRecord。"""
        item = self._records[faiss_id]
        from agent_core.documents import Document
        from agent_core.retrieval._common import decode_record_payload

        return decode_record_payload(
            item["payload"],
            record_id=str(item["record_id"]),
            vector=tuple(float(value) for value in item["vector"]),
            namespace=str(item["namespace"]),
            user_id=item.get("user_id"),
            tenant_id=item.get("tenant_id"),
        )

    def _check_record_access_locked(
        self, faiss_id: int, access: AccessContext | None
    ) -> None:
        """内部删除前做权限校验。"""
        if access is None:
            return
        record = self._deserialize_record(faiss_id)
        check_record_access(record, access)

    def _check_required_access(self, access: AccessContext | None) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 VectorStore 要求提供 AccessContext")

    def _read_metadata(self) -> dict[int, dict[str, Any]]:
        """读取 metadata sidecar；索引文件存在但 sidecar 缺失视为损坏。"""
        if not self._meta_file.exists():
            raise ValueError(f"FAISS metadata 文件缺失：{self._meta_file}")
        raw = json.loads(self._meta_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("records"), list):
            raise ValueError(f"FAISS metadata 格式无效：{self._meta_file}")
        return {int(item["faiss_id"]): dict(item) for item in raw["records"]}

    def _persist_locked(self) -> None:
        """同步保存索引和 metadata；调用方必须已经持有 self._lock。"""
        import faiss

        if self._index is None:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(self._index_file))
        payload = {
            "collection_name": self._collection_name,
            "dimension": self._dimension,
            "next_id": self._next_id,
            "records": [
                {"faiss_id": faiss_id, **item}
                for faiss_id, item in self._records.items()
            ],
        }
        self._meta_file.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
