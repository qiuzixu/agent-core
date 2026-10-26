"""Qdrant 向量数据库适配器。

Qdrant 是独立部署的向量数据库服务（同时提供本地嵌入式持久化模式），
适合生产多实例共享；单机开发仍建议 FAISS / Chroma 这类零部署后端。

与 Chroma 适配器保持同一套语义：
- namespace / 访问权限 / 业务 metadata 存放在 payload 的保留键下；
- 检索时 namespace 与访问范围下推给 Qdrant 过滤，业务 metadata 过滤
  在 Python 侧精确匹配（与 Chroma 实现语义一致）；
- qdrant-client 是同步 SDK，所有 IO 通过 asyncio.to_thread 执行，
  避免阻塞 Agent 所在的事件循环。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from agent_core.access import AccessContext
from agent_core.retrieval._common import (
    decode_record_payload,
    encode_record_payload,
    metadata_matches,
    normalize_record,
    record_accessible,
    validate_vector,
)
from agent_core.retrieval.core import VectorStore
from agent_core.retrieval.types import VectorQuery, VectorRecord, VectorSearchResult

# payload 保留键：用前缀避免与业务 metadata 冲突
_NAMESPACE_KEY = "_agent_core_namespace"
_USER_KEY = "_agent_core_user_id"
_TENANT_KEY = "_agent_core_tenant_id"
_PAYLOAD_KEY = "_agent_core_payload"
_RECORD_ID_KEY = "_agent_core_record_id"
_PUBLIC_OWNER = ""

# 带业务 metadata 过滤时无法下推，需要超额拉取后在 Python 侧精确过滤；
# 这是服务器模式下的折中：正确性优先，超大规模 + 复杂过滤场景建议走 pgvector SQL。
_FALLBACK_FETCH_LIMIT = 1000


class QdrantVectorStore(VectorStore):
    """基于 qdrant-client 的向量存储实现。

    三种连接方式（按优先级）：
    1. ``client`` 注入 —— 测试或复用外部连接池；
    2. ``url`` —— 连接独立部署的 Qdrant 服务（生产推荐）；
    3. ``path`` —— 本地嵌入式持久化（单机开发，无需部署服务）。
    """

    def __init__(
        self,
        path: str | Path = "./qdrant_db",
        *,
        collection_name: str = "agent_core",
        require_access: bool = False,
        client: Any | None = None,
        url: str | None = None,
        api_key: str | None = None,
        dimension: int | None = None,
    ) -> None:
        """构造 Qdrant 存储；显式注入 client 主要用于测试。"""
        if not collection_name.strip():
            raise ValueError("collection_name 不能为空")
        if dimension is not None and dimension <= 0:
            raise ValueError("dimension 必须大于 0")

        owns_client = client is None
        if client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as exc:
                raise ImportError(
                    "QdrantVectorStore 需要 qdrant-client，请安装 zero-agent-core[qdrant]"
                ) from exc
            if url:
                client = QdrantClient(url=url, api_key=api_key)
            else:
                client = QdrantClient(path=str(Path(path).resolve()))

        self._client: Any = client
        self._owns_client = owns_client
        self._collection_name = collection_name
        self._require_access = require_access
        self._dimension = dimension

    async def close(self) -> None:
        """关闭由当前 Store 创建的客户端，释放本地文件句柄或连接。"""
        if not self._owns_client:
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            await self._call("close")
        self._owns_client = False

    async def upsert(
        self,
        records: Any,
        *,
        access: AccessContext | None = None,
    ) -> list[str]:
        values = [
            normalize_record(record, access, require_access=self._require_access)
            for record in records
        ]
        if not values:
            return []
        for record in values:
            validate_vector(record.vector, dimension=self._dimension)
        await self._check_existing_access([record.record_id for record in values], access)
        # Qdrant 建集合时必须固定向量维度，取第一批向量确定
        await self._ensure_collection(len(values[0].vector))
        points = [self._point(record) for record in values]
        await self._call(
            "upsert",
            self._collection_name,
            points=points,
            wait=True,
        )
        return [record.record_id for record in values]

    async def delete(
        self,
        record_ids: Any,
        *,
        access: AccessContext | None = None,
    ) -> int:
        self._check_required_access(access)
        if not record_ids:
            return 0
        if not await self._call("collection_exists", self._collection_name):
            return 0
        stored = await self._call(
            "retrieve",
            self._collection_name,
            ids=[self._point_id(record_id) for record_id in record_ids],
            with_payload=True,
            with_vectors=False,
        )
        allowed: list[str] = []
        for item in stored or []:
            payload = dict(getattr(item, "payload", None) or {})
            record_id = str(payload.get(_RECORD_ID_KEY) or getattr(item, "id", ""))
            self._check_metadata_access(record_id, payload, access)
            allowed.append(record_id)
        if allowed:
            await self._call(
                "delete",
                self._collection_name,
                points_selector=[self._point_id(record_id) for record_id in allowed],
                wait=True,
            )
        return len(allowed)

    async def search(self, query: VectorQuery) -> list[VectorSearchResult]:
        self._check_required_access(query.access)
        validate_vector(query.vector)
        # 集合不存在时返回空结果，而不是抛错——与 InMemory/Chroma 行为一致
        if not await self._call("collection_exists", self._collection_name):
            return []
        fetch_limit, query_filter = self._filter_plan(query)
        response = await self._call(
            "query_points",
            self._collection_name,
            query=list(query.vector),
            limit=fetch_limit,
            query_filter=query_filter,
            with_payload=True,
            with_vectors=True,
        )
        points = getattr(response, "points", None) or []
        matches: list[VectorSearchResult] = []
        for item in points:
            payload = dict(getattr(item, "payload", None) or {})
            record = self._record_from_payload(item, payload)
            combined_metadata = {**record.document.metadata, **record.metadata}
            if query.metadata_filter and not metadata_matches(
                combined_metadata, query.metadata_filter
            ):
                continue
            if not record_accessible(record, query.access):
                continue
            score = float(getattr(item, "score", 0.0))
            if query.min_score is not None and score < query.min_score:
                continue
            matches.append(VectorSearchResult(record=record, score=score))
            if len(matches) >= query.limit:
                break
        return matches

    # ──────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """把同步 SDK 调用丢进线程池，避免阻塞事件循环。"""
        return await asyncio.to_thread(getattr(self._client, method), *args, **kwargs)

    async def _ensure_collection(self, size: int) -> None:
        """按需创建 collection；已存在时交给 Qdrant 自己校验维度一致性。"""
        if self._dimension is not None:
            size = self._dimension
        if await self._call("collection_exists", self._collection_name):
            return
        await self._call(
            "create_collection",
            self._collection_name,
            vectors_config={"size": size, "distance": "Cosine"},
        )

    async def _check_existing_access(
        self,
        record_ids: Any,
        access: AccessContext | None,
    ) -> None:
        """覆盖写入前校验既有记录归属，防止越权替换他人数据。"""
        if access is None:
            return
        if not await self._call("collection_exists", self._collection_name):
            return
        stored = await self._call(
            "retrieve",
            self._collection_name,
            ids=[self._point_id(record_id) for record_id in record_ids],
            with_payload=True,
            with_vectors=False,
        )
        for item in stored or []:
            payload = dict(getattr(item, "payload", None) or {})
            record_id = str(payload.get(_RECORD_ID_KEY) or getattr(item, "id", ""))
            self._check_metadata_access(record_id, payload, access)

    @staticmethod
    def _point_id(record_id: str) -> str:
        """Qdrant 点 ID 只接受 UUID 或整数；用 uuid5 把业务 ID 确定性地映射为 UUID。

        同一 record_id 永远映射到同一 UUID，天然支持 upsert 覆盖语义。
        """
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-core:{record_id}"))

    @staticmethod
    def _point(record: VectorRecord) -> dict[str, Any]:
        """把 VectorRecord 编码为 Qdrant PointStruct（REST 兼容 dict 形式）。"""
        return {
            "id": QdrantVectorStore._point_id(record.record_id),
            "vector": list(record.vector),
            "payload": {
                _RECORD_ID_KEY: record.record_id,
                _NAMESPACE_KEY: record.namespace,
                _USER_KEY: record.user_id or _PUBLIC_OWNER,
                _TENANT_KEY: record.tenant_id or _PUBLIC_OWNER,
                _PAYLOAD_KEY: encode_record_payload(record),
            },
        }

    def _filter_plan(self, query: VectorQuery) -> tuple[int, dict[str, Any] | None]:
        """规划检索的下推过滤与拉取条数。

        namespace 与访问范围下推给 Qdrant；业务 metadata 过滤因为存放在
        payload JSON 串里无法下推，需要超额拉取后精确匹配（与 Chroma 一致）。
        """
        must: list[dict[str, Any]] = [
            {"key": _NAMESPACE_KEY, "match": {"value": query.namespace}}
        ]
        access = query.access
        if access is not None:
            must.append(
                {
                    "should": [
                        {"key": _TENANT_KEY, "match": {"value": _PUBLIC_OWNER}},
                        {"key": _TENANT_KEY, "match": {"value": access.tenant_id or _PUBLIC_OWNER}},
                    ]
                }
            )
            if not access.is_admin:
                must.append(
                    {
                        "should": [
                            {"key": _USER_KEY, "match": {"value": _PUBLIC_OWNER}},
                            {"key": _USER_KEY, "match": {"value": access.user_id or _PUBLIC_OWNER}},
                        ]
                    }
                )
        query_filter = {"must": must}
        # 无业务过滤时只需拉 limit 条；有过滤时超额拉取再精确筛选
        fetch_limit = query.limit if not query.metadata_filter else _FALLBACK_FETCH_LIMIT
        return fetch_limit, query_filter

    @staticmethod
    def _record_from_payload(item: Any, payload: dict[str, Any]) -> VectorRecord:
        """从 Qdrant 命中结果还原完整 VectorRecord。"""
        return decode_record_payload(
            payload.get(_PAYLOAD_KEY),
            record_id=str(payload.get(_RECORD_ID_KEY) or getattr(item, "id", "")),
            vector=tuple(float(value) for value in (getattr(item, "vector", None) or [])),
            namespace=str(payload.get(_NAMESPACE_KEY, "default")),
            user_id=str(payload[_USER_KEY]) if payload.get(_USER_KEY) else None,
            tenant_id=str(payload[_TENANT_KEY]) if payload.get(_TENANT_KEY) else None,
        )

    @staticmethod
    def _check_metadata_access(
        record_id: str,
        metadata: dict[str, Any],
        access: AccessContext | None,
    ) -> None:
        """删除/覆盖前校验记录归属，拒绝越权操作。"""
        if access is None:
            return
        user_id = str(metadata[_USER_KEY]) if metadata.get(_USER_KEY) else None
        tenant_id = str(metadata[_TENANT_KEY]) if metadata.get(_TENANT_KEY) else None
        if not access.can_access(user_id, tenant_id):
            raise PermissionError(f"无权访问向量记录：{record_id}")

    def _check_required_access(self, access: AccessContext | None) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 VectorStore 要求提供 AccessContext")


__all__ = ["QdrantVectorStore"]
