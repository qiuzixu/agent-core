"""进程内轻量知识图谱存储实现。

当前实现刻意保持零外部依赖：实体/关系保存在进程内，重启即丢失。
后续可按同一 ``KnowledgeGraphStore`` 协议扩展 SQLite 或 PostgreSQL。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable
from typing import Any

from agent_core.ports.knowledge_graph import (
    Direction,
    GraphEntity,
    GraphRelation,
)


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class InMemoryKnowledgeGraphStore:
    """进程内图谱存储；同一租户下实体全局共享，线程级索引用于会话追溯。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entities: dict[tuple[str, str], GraphEntity] = {}
        self._relations: dict[tuple[str, str], GraphRelation] = {}
        self._thread_entities: dict[tuple[str, str], set[str]] = {}
        self._thread_relations: dict[tuple[str, str], set[str]] = {}

    async def add_entity(
        self,
        entity_type: str,
        value: str,
        *,
        entity_id: str | None = None,
        display_name: str | None = None,
        properties: dict[str, Any] | None = None,
        source: str | None = None,
        thread_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_type = entity_type.strip().lower()
        normalized_value = value.strip()
        if not normalized_type or not normalized_value:
            raise ValueError("entity_type 和 value 不能为空")
        tenant = (tenant_id or "default").strip() or "default"
        resolved_id = (entity_id or "").strip() or f"{normalized_type}:{normalized_value}"
        key = (tenant, resolved_id)
        now = _now_iso()
        with self._lock:
            existing = self._entities.get(key)
            if existing is None:
                entity = GraphEntity(
                    entity_id=resolved_id,
                    entity_type=normalized_type,
                    value=normalized_value,
                    display_name=(display_name or normalized_value).strip(),
                    properties=dict(properties or {}),
                    source=(source or "api").strip(),
                    created_at=now,
                    updated_at=now,
                )
                self._entities[key] = entity
            else:
                existing.display_name = (display_name or existing.display_name or existing.value).strip()
                existing.properties = {**existing.properties, **(properties or {})}
                existing.source = existing.source or (source or "")
                existing.updated_at = now
                entity = existing
            if thread_id:
                self._thread_entities.setdefault((tenant, thread_id.strip()), set()).add(resolved_id)
        result = entity.to_dict()
        result["user_id"] = user_id or None
        return result

    async def add_relation(
        self,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
        *,
        properties: dict[str, Any] | None = None,
        source: str | None = None,
        thread_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        tenant = (tenant_id or "default").strip() or "default"
        src = source_entity_id.strip()
        dst = target_entity_id.strip()
        normalized_type = relation_type.strip().upper()
        if not src or not dst or not normalized_type:
            raise ValueError("source_entity_id、target_entity_id 和 relation_type 不能为空")
        relation_id = self._stable_relation_id(tenant, src, dst, normalized_type)
        key = (tenant, relation_id)
        now = _now_iso()
        with self._lock:
            tenant_entity_ids = {
                entity_id
                for key_tenant, entity_id in self._entities
                if key_tenant == tenant
            }
            if src not in tenant_entity_ids:
                raise ValueError(f"source entity 不存在: {src}")
            if dst not in tenant_entity_ids:
                raise ValueError(f"target entity 不存在: {dst}")
            existing = self._relations.get(key)
            if existing is None:
                relation = GraphRelation(
                    relation_id=relation_id,
                    source_entity_id=src,
                    target_entity_id=dst,
                    relation_type=normalized_type,
                    properties=dict(properties or {}),
                    source=(source or "api").strip(),
                    created_at=now,
                    updated_at=now,
                )
                self._relations[key] = relation
            else:
                existing.properties = {**existing.properties, **(properties or {})}
                existing.source = existing.source or (source or "")
                existing.updated_at = now
                relation = existing
            if thread_id:
                self._thread_relations.setdefault((tenant, thread_id.strip()), set()).add(relation_id)
        result = relation.to_dict()
        result["user_id"] = user_id or None
        return result

    async def get_entity(
        self,
        entity_id: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        tenant = (tenant_id or "default").strip() or "default"
        with self._lock:
            entity = self._entities.get((tenant, entity_id.strip()))
        return entity.to_dict() if entity is not None else None

    async def query_subgraph(
        self,
        start_entity_id: str,
        *,
        max_hops: int = 1,
        direction: Direction = "outgoing",
        relation_types: Iterable[str] | None = None,
        entity_types: Iterable[str] | None = None,
        tenant_id: str | None = None,
        limit_entities: int = 50,
        limit_relations: int = 200,
    ) -> dict[str, Any]:
        tenant = (tenant_id or "default").strip() or "default"
        start = start_entity_id.strip()
        allowed_relations = {item.strip().upper() for item in (relation_types or []) if item.strip()}
        allowed_entities = {item.strip().lower() for item in (entity_types or []) if item.strip()}
        with self._lock:
            start_entity = self._entities.get((tenant, start))
            if start_entity is None:
                return {"start_entity_id": start, "entities": [], "relations": [], "hops": 0}
            entities: dict[str, GraphEntity] = {start: start_entity}
            relations: dict[str, GraphRelation] = {}
            frontier = {start}
            for _ in range(max(0, max_hops)):
                next_frontier: set[str] = set()
                for relation in self._relations.values():
                    if relation.relation_id in relations:
                        continue
                    traversed = False
                    if direction in {"outgoing", "both"} and relation.source_entity_id in frontier:
                        traversed = True
                    if direction in {"incoming", "both"} and relation.target_entity_id in frontier:
                        traversed = True
                    if not traversed:
                        continue
                    if allowed_relations and relation.relation_type not in allowed_relations:
                        continue
                    relations[relation.relation_id] = relation
                    next_frontier.add(relation.source_entity_id)
                    next_frontier.add(relation.target_entity_id)
                for entity_id in next_frontier:
                    entity = self._entities.get((tenant, entity_id))
                    if entity is not None and (
                        not allowed_entities or entity.entity_type in allowed_entities
                    ):
                        entities[entity_id] = entity
                frontier = next_frontier
                if len(entities) >= limit_entities or len(relations) >= limit_relations:
                    break
        return {
            "start_entity_id": start,
            "entities": [item.to_dict() for item in list(entities.values())[:limit_entities]],
            "relations": [item.to_dict() for item in list(relations.values())[:limit_relations]],
            "hops": max(0, max_hops),
        }

    @staticmethod
    def _stable_relation_id(
        tenant: str, source_entity_id: str, target_entity_id: str, relation_type: str
    ) -> str:
        raw = "|".join((tenant, source_entity_id, target_entity_id, relation_type))
        return "rel-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]