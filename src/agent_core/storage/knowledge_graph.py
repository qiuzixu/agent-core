"""进程内轻量知识图谱存储实现。

当前实现刻意保持零外部依赖：实体/关系保存在进程内，重名时丢失。
后续对齐同一 ``KnowledgeGraphStore`` 协议扩展 SQLite 或 PostgreSQL。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agent_core.ports.knowledge_graph import (
    Direction,
    GraphEntity,
    GraphRelation,
    GraphSubgraph,
)
from agent_core.storage._sqlite_utils import (
    _run_in_thread,
    run_sync_in_thread,
    sqlite_connection,
)


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _traverse_graph(
    *,
    start: str,
    entities: dict[str, GraphEntity],
    relations: list[GraphRelation],
    max_hops: int,
    direction: Direction,
    relation_types: Iterable[str] | None,
    entity_types: Iterable[str] | None,
    limit_entities: int,
    limit_relations: int,
) -> GraphSubgraph:
    """在已加载的租户子图上做受限 BFS 遍历。"""
    allowed_relations = {
        item.strip().upper() for item in (relation_types or []) if item.strip()
    }
    allowed_entities = {
        item.strip().lower() for item in (entity_types or []) if item.strip()
    }
    start_entity = entities.get(start)
    if start_entity is None:
        return GraphSubgraph(start_entity_id=start, entities=[], relations=[], hops=0)

    selected: dict[str, GraphEntity] = {start: start_entity}
    selected_relations: dict[str, GraphRelation] = {}
    frontier = {start}
    for _ in range(max(0, max_hops)):
        next_frontier: set[str] = set()
        for relation in relations:
            if relation.relation_id in selected_relations:
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
            selected_relations[relation.relation_id] = relation
            next_frontier.add(relation.source_entity_id)
            next_frontier.add(relation.target_entity_id)
        for entity_id in next_frontier:
            entity = entities.get(entity_id)
            if entity is not None and (
                not allowed_entities or entity.entity_type in allowed_entities
            ):
                selected[entity_id] = entity
        frontier = next_frontier
        if len(selected) >= limit_entities or len(selected_relations) >= limit_relations:
            break
    return GraphSubgraph(
        start_entity_id=start,
        entities=list(selected.values())[:limit_entities],
        relations=list(selected_relations.values())[:limit_relations],
        hops=max(0, max_hops),
    )


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
    ) -> GraphEntity:
        normalized_type = entity_type.strip().lower()
        normalized_value = value.strip()
        if not normalized_type or not normalized_value:
            raise ValueError("entity_type 或 value 不能为空")
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
        return entity

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
    ) -> GraphRelation:
        tenant = (tenant_id or "default").strip() or "default"
        src = source_entity_id.strip()
        dst = target_entity_id.strip()
        normalized_type = relation_type.strip().upper()
        if not src or not dst or not normalized_type:
            raise ValueError("source_entity_id、target_entity_id 或 relation_type 不能为空")
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
                raise ValueError(f"source entity 不存在：{src}")
            if dst not in tenant_entity_ids:
                raise ValueError(f"target entity 不存在：{dst}")
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
        return relation

    async def get_entity(
        self,
        entity_id: str,
        *,
        tenant_id: str | None = None,
    ) -> GraphEntity | None:
        tenant = (tenant_id or "default").strip() or "default"
        with self._lock:
            entity = self._entities.get((tenant, entity_id.strip()))
        return entity

    async def search_entities(
        self,
        query: str,
        *,
        limit: int = 20,
        tenant_id: str | None = None,
    ) -> list[GraphEntity]:
        normalized = query.strip().lower()
        if not normalized:
            return []
        tenant = (tenant_id or "default").strip() or "default"
        with self._lock:
            matches: list[GraphEntity] = []
            for (entity_tenant, _), entity in self._entities.items():
                if entity_tenant != tenant:
                    continue
                fields = (
                    entity.entity_id.lower(),
                    entity.value.lower(),
                    entity.display_name.lower(),
                )
                # 双向包含：既支持“查询词是实体的一部分”，也支持“实体出现在整句里”。
                if any(normalized in field for field in fields) or any(
                    len(field) >= 2 and field in normalized for field in fields
                ):
                    matches.append(entity)
            return matches[: max(1, limit)]

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
    ) -> GraphSubgraph:
        tenant = (tenant_id or "default").strip() or "default"
        start = start_entity_id.strip()
        allowed_relations = {item.strip().upper() for item in (relation_types or []) if item.strip()}
        allowed_entities = {item.strip().lower() for item in (entity_types or []) if item.strip()}
        with self._lock:
            start_entity = self._entities.get((tenant, start))
            if start_entity is None:
                return GraphSubgraph(start_entity_id=start, entities=[], relations=[], hops=0)
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
        return GraphSubgraph(
            start_entity_id=start,
            entities=list(entities.values())[:limit_entities],
            relations=list(relations.values())[:limit_relations],
            hops=max(0, max_hops),
        )

    @staticmethod
    def _stable_relation_id(
        tenant: str, source_entity_id: str, target_entity_id: str, relation_type: str
    ) -> str:
        raw = "|".join((tenant, source_entity_id, target_entity_id, relation_type))
        return "rel-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

class SqliteKnowledgeGraphStore:
    """基于 SQLite 的知识图谱存储；用于开发与单实例部署。"""

    def __init__(self, db_path: str | Path = "./sessions.db") -> None:
        self._db_path = str(Path(db_path).resolve())
        run_sync_in_thread(self._init_db)

    def _init_db(self) -> None:
        with sqlite_connection(self._db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kg_entities (
                    tenant_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    properties TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, entity_id)
                );
                CREATE TABLE IF NOT EXISTS kg_relations (
                    tenant_id TEXT NOT NULL,
                    relation_id TEXT NOT NULL,
                    source_entity_id TEXT NOT NULL,
                    target_entity_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    properties TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, relation_id)
                );
                CREATE TABLE IF NOT EXISTS kg_thread_entities (
                    tenant_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, thread_id, entity_id)
                );
                CREATE TABLE IF NOT EXISTS kg_thread_relations (
                    tenant_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    relation_id TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, thread_id, relation_id)
                );
                """
            )

    @staticmethod
    def _entity_row(row: tuple[Any, ...]) -> GraphEntity:
        return GraphEntity(
            entity_id=row[1],
            entity_type=row[2],
            value=row[3],
            display_name=row[4],
            properties=json.loads(row[5] or "{}"),
            source=row[6],
            created_at=row[7],
            updated_at=row[8],
        )

    @staticmethod
    def _relation_row(row: tuple[Any, ...]) -> GraphRelation:
        return GraphRelation(
            relation_id=row[1],
            source_entity_id=row[2],
            target_entity_id=row[3],
            relation_type=row[4],
            properties=json.loads(row[5] or "{}"),
            source=row[6],
            created_at=row[7],
            updated_at=row[8],
        )

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
    ) -> GraphEntity:
        normalized_type = entity_type.strip().lower()
        normalized_value = value.strip()
        if not normalized_type or not normalized_value:
            raise ValueError("entity_type 或 value 不能为空")
        tenant = (tenant_id or "default").strip() or "default"
        resolved_id = (entity_id or "").strip() or f"{normalized_type}:{normalized_value}"
        now = _now_iso()

        def _op() -> GraphEntity:
            with sqlite_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM kg_entities WHERE tenant_id = ? AND entity_id = ?",
                    (tenant, resolved_id),
                ).fetchone()
                if row is None:
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
                    conn.execute(
                        """
                        INSERT INTO kg_entities
                        (tenant_id, entity_id, entity_type, value, display_name,
                         properties, source, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tenant,
                            entity.entity_id,
                            entity.entity_type,
                            entity.value,
                            entity.display_name,
                            json.dumps(entity.properties, ensure_ascii=False),
                            entity.source,
                            entity.created_at,
                            entity.updated_at,
                        ),
                    )
                else:
                    current = self._entity_row(row)
                    current.display_name = (
                        display_name or current.display_name or current.value
                    ).strip()
                    current.properties = {**current.properties, **(properties or {})}
                    current.source = current.source or (source or "")
                    current.updated_at = now
                    entity = current
                    conn.execute(
                        """
                        UPDATE kg_entities
                        SET display_name = ?, properties = ?, source = ?, updated_at = ?
                        WHERE tenant_id = ? AND entity_id = ?
                        """,
                        (
                            entity.display_name,
                            json.dumps(entity.properties, ensure_ascii=False),
                            entity.source,
                            entity.updated_at,
                            tenant,
                            entity.entity_id,
                        ),
                    )
                if thread_id:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO kg_thread_entities (tenant_id, thread_id, entity_id)
                        VALUES (?, ?, ?)
                        """,
                        (tenant, thread_id.strip(), resolved_id),
                    )
                return entity

        return await _run_in_thread(_op)

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
    ) -> GraphRelation:
        tenant = (tenant_id or "default").strip() or "default"
        src = source_entity_id.strip()
        dst = target_entity_id.strip()
        normalized_type = relation_type.strip().upper()
        if not src or not dst or not normalized_type:
            raise ValueError("source_entity_id、target_entity_id 或 relation_type 不能为空")
        relation_id = self._stable_relation_id(tenant, src, dst, normalized_type)
        now = _now_iso()

        def _op() -> GraphRelation:
            with sqlite_connection(self._db_path) as conn:
                source_count = conn.execute(
                    "SELECT COUNT(*) FROM kg_entities WHERE tenant_id = ? AND entity_id = ?",
                    (tenant, src),
                ).fetchone()[0]
                target_count = conn.execute(
                    "SELECT COUNT(*) FROM kg_entities WHERE tenant_id = ? AND entity_id = ?",
                    (tenant, dst),
                ).fetchone()[0]
                if source_count == 0:
                    raise ValueError(f"source entity 不存在：{src}")
                if target_count == 0:
                    raise ValueError(f"target entity 不存在：{dst}")
                row = conn.execute(
                    "SELECT * FROM kg_relations WHERE tenant_id = ? AND relation_id = ?",
                    (tenant, relation_id),
                ).fetchone()
                if row is None:
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
                    conn.execute(
                        """
                        INSERT INTO kg_relations
                        (tenant_id, relation_id, source_entity_id, target_entity_id,
                         relation_type, properties, source, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tenant,
                            relation.relation_id,
                            relation.source_entity_id,
                            relation.target_entity_id,
                            relation.relation_type,
                            json.dumps(relation.properties, ensure_ascii=False),
                            relation.source,
                            relation.created_at,
                            relation.updated_at,
                        ),
                    )
                else:
                    current = self._relation_row(row)
                    current.properties = {**current.properties, **(properties or {})}
                    current.source = current.source or (source or "")
                    current.updated_at = now
                    relation = current
                    conn.execute(
                        """
                        UPDATE kg_relations
                        SET properties = ?, source = ?, updated_at = ?
                        WHERE tenant_id = ? AND relation_id = ?
                        """,
                        (
                            json.dumps(relation.properties, ensure_ascii=False),
                            relation.source,
                            relation.updated_at,
                            tenant,
                            relation.relation_id,
                        ),
                    )
                if thread_id:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO kg_thread_relations (tenant_id, thread_id, relation_id)
                        VALUES (?, ?, ?)
                        """,
                        (tenant, thread_id.strip(), relation_id),
                    )
                return relation

        return await _run_in_thread(_op)

    async def get_entity(
        self,
        entity_id: str,
        *,
        tenant_id: str | None = None,
    ) -> GraphEntity | None:
        tenant = (tenant_id or "default").strip() or "default"

        def _op() -> GraphEntity | None:
            with sqlite_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM kg_entities WHERE tenant_id = ? AND entity_id = ?",
                    (tenant, entity_id.strip()),
                ).fetchone()
            return self._entity_row(row) if row is not None else None

        return await _run_in_thread(_op)

    async def search_entities(
        self,
        query: str,
        *,
        limit: int = 20,
        tenant_id: str | None = None,
    ) -> list[GraphEntity]:
        normalized = query.strip().lower()
        if not normalized:
            return []
        tenant = (tenant_id or "default").strip() or "default"

        def _op() -> list[GraphEntity]:
            pattern = f"%{normalized}%"
            with sqlite_connection(self._db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM kg_entities
                    WHERE tenant_id = ? AND (
                        lower(entity_id) LIKE ? OR lower(value) LIKE ? OR lower(display_name) LIKE ?
                        OR (length(value) >= 2 AND ? LIKE '%' || lower(value) || '%')
                        OR (length(display_name) >= 2 AND ? LIKE '%' || lower(display_name) || '%')
                    )
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (tenant, pattern, pattern, pattern, normalized, normalized, max(1, limit)),
                ).fetchall()
            return [self._entity_row(row) for row in rows]

        return await _run_in_thread(_op)

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
    ) -> GraphSubgraph:
        tenant = (tenant_id or "default").strip() or "default"
        start = start_entity_id.strip()

        def _op() -> GraphSubgraph:
            with sqlite_connection(self._db_path) as conn:
                entity_rows = conn.execute(
                    "SELECT * FROM kg_entities WHERE tenant_id = ?", (tenant,)
                ).fetchall()
                relation_rows = conn.execute(
                    "SELECT * FROM kg_relations WHERE tenant_id = ?", (tenant,)
                ).fetchall()
            entities = {row[1]: self._entity_row(row) for row in entity_rows}
            relations = [self._relation_row(row) for row in relation_rows]
            return _traverse_graph(
                start=start,
                entities=entities,
                relations=relations,
                max_hops=max_hops,
                direction=direction,
                relation_types=relation_types,
                entity_types=entity_types,
                limit_entities=limit_entities,
                limit_relations=limit_relations,
            )

        return await _run_in_thread(_op)

    @staticmethod
    def _stable_relation_id(
        tenant: str,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
    ) -> str:
        raw = "|".join((tenant, source_entity_id, target_entity_id, relation_type))
        return "rel-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class PostgresKnowledgeGraphStore:
    """基于 PostgreSQL 的知识图谱存储；用于生产与多实例部署。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._pool: Any = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        import asyncpg

        self._pool = await asyncpg.create_pool(dsn=self._dsn)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _check(self) -> None:
        if self._pool is None:
            raise RuntimeError("请先调用 initialize()")

    @staticmethod
    def _entity_row(row: Any) -> GraphEntity:
        return GraphEntity(
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            value=row["value"],
            display_name=row["display_name"],
            properties=json.loads(row["properties"] or "{}"),
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _relation_row(row: Any) -> GraphRelation:
        return GraphRelation(
            relation_id=row["relation_id"],
            source_entity_id=row["source_entity_id"],
            target_entity_id=row["target_entity_id"],
            relation_type=row["relation_type"],
            properties=json.loads(row["properties"] or "{}"),
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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
    ) -> GraphEntity:
        self._check()
        normalized_type = entity_type.strip().lower()
        normalized_value = value.strip()
        if not normalized_type or not normalized_value:
            raise ValueError("entity_type 或 value 不能为空")
        tenant = (tenant_id or "default").strip() or "default"
        resolved_id = (entity_id or "").strip() or f"{normalized_type}:{normalized_value}"
        now = _now_iso()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM kg_entities WHERE tenant_id = $1 AND entity_id = $2",
                tenant,
                resolved_id,
            )
            if row is None:
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
                await conn.execute(
                    """
                    INSERT INTO kg_entities
                    (tenant_id, entity_id, entity_type, value, display_name,
                     properties, source, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
                    """,
                    tenant,
                    entity.entity_id,
                    entity.entity_type,
                    entity.value,
                    entity.display_name,
                    json.dumps(entity.properties, ensure_ascii=False),
                    entity.source,
                    entity.created_at,
                    entity.updated_at,
                )
            else:
                current = self._entity_row(row)
                current.display_name = (
                    display_name or current.display_name or current.value
                ).strip()
                current.properties = {**current.properties, **(properties or {})}
                current.source = current.source or (source or "")
                current.updated_at = now
                entity = current
                await conn.execute(
                    """
                    UPDATE kg_entities
                    SET display_name = $1, properties = $2::jsonb, source = $3, updated_at = $4
                    WHERE tenant_id = $5 AND entity_id = $6
                    """,
                    entity.display_name,
                    json.dumps(entity.properties, ensure_ascii=False),
                    entity.source,
                    entity.updated_at,
                    tenant,
                    entity.entity_id,
                )
            if thread_id:
                await conn.execute(
                    """
                    INSERT INTO kg_thread_entities (tenant_id, thread_id, entity_id)
                    VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
                    """,
                    tenant,
                    thread_id.strip(),
                    resolved_id,
                )
        return entity

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
    ) -> GraphRelation:
        self._check()
        tenant = (tenant_id or "default").strip() or "default"
        src = source_entity_id.strip()
        dst = target_entity_id.strip()
        normalized_type = relation_type.strip().upper()
        if not src or not dst or not normalized_type:
            raise ValueError("source_entity_id、target_entity_id 或 relation_type 不能为空")
        relation_id = SqliteKnowledgeGraphStore._stable_relation_id(
            tenant, src, dst, normalized_type
        )
        now = _now_iso()
        async with self._pool.acquire() as conn:
            source_exists = await conn.fetchval(
                "SELECT 1 FROM kg_entities WHERE tenant_id = $1 AND entity_id = $2",
                tenant,
                src,
            )
            target_exists = await conn.fetchval(
                "SELECT 1 FROM kg_entities WHERE tenant_id = $1 AND entity_id = $2",
                tenant,
                dst,
            )
            if source_exists is None:
                raise ValueError(f"source entity 不存在：{src}")
            if target_exists is None:
                raise ValueError(f"target entity 不存在：{dst}")
            row = await conn.fetchrow(
                "SELECT * FROM kg_relations WHERE tenant_id = $1 AND relation_id = $2",
                tenant,
                relation_id,
            )
            if row is None:
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
                await conn.execute(
                    """
                    INSERT INTO kg_relations
                    (tenant_id, relation_id, source_entity_id, target_entity_id,
                     relation_type, properties, source, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
                    """,
                    tenant,
                    relation.relation_id,
                    relation.source_entity_id,
                    relation.target_entity_id,
                    relation.relation_type,
                    json.dumps(relation.properties, ensure_ascii=False),
                    relation.source,
                    relation.created_at,
                    relation.updated_at,
                )
            else:
                current = self._relation_row(row)
                current.properties = {**current.properties, **(properties or {})}
                current.source = current.source or (source or "")
                current.updated_at = now
                relation = current
                await conn.execute(
                    """
                    UPDATE kg_relations
                    SET properties = $1::jsonb, source = $2, updated_at = $3
                    WHERE tenant_id = $4 AND relation_id = $5
                    """,
                    json.dumps(relation.properties, ensure_ascii=False),
                    relation.source,
                    relation.updated_at,
                    tenant,
                    relation.relation_id,
                )
            if thread_id:
                await conn.execute(
                    """
                    INSERT INTO kg_thread_relations (tenant_id, thread_id, relation_id)
                    VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
                    """,
                    tenant,
                    thread_id.strip(),
                    relation_id,
                )
        return relation

    async def get_entity(
        self,
        entity_id: str,
        *,
        tenant_id: str | None = None,
    ) -> GraphEntity | None:
        self._check()
        tenant = (tenant_id or "default").strip() or "default"
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM kg_entities WHERE tenant_id = $1 AND entity_id = $2",
                tenant,
                entity_id.strip(),
            )
        return self._entity_row(row) if row is not None else None

    async def search_entities(
        self,
        query: str,
        *,
        limit: int = 20,
        tenant_id: str | None = None,
    ) -> list[GraphEntity]:
        self._check()
        normalized = query.strip().lower()
        if not normalized:
            return []
        tenant = (tenant_id or "default").strip() or "default"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM kg_entities
                WHERE tenant_id = $1 AND (
                    entity_id ILIKE $2 OR value ILIKE $2 OR display_name ILIKE $2
                    OR (length(value) >= 2 AND $4 ILIKE '%' || value || '%')
                    OR (length(display_name) >= 2 AND $4 ILIKE '%' || display_name || '%')
                )
                ORDER BY updated_at DESC LIMIT $3
                """,
                tenant,
                f"%{normalized}%",
                max(1, limit),
                normalized,
            )
        return [self._entity_row(row) for row in rows]

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
    ) -> GraphSubgraph:
        self._check()
        tenant = (tenant_id or "default").strip() or "default"
        start = start_entity_id.strip()
        async with self._pool.acquire() as conn:
            entity_rows = await conn.fetch(
                "SELECT * FROM kg_entities WHERE tenant_id = $1", tenant
            )
            relation_rows = await conn.fetch(
                "SELECT * FROM kg_relations WHERE tenant_id = $1", tenant
            )
        entities = {row["entity_id"]: self._entity_row(row) for row in entity_rows}
        relations = [self._relation_row(row) for row in relation_rows]
        return _traverse_graph(
            start=start,
            entities=entities,
            relations=relations,
            max_hops=max_hops,
            direction=direction,
            relation_types=relation_types,
            entity_types=entity_types,
            limit_entities=limit_entities,
            limit_relations=limit_relations,
        )


def create_knowledge_graph_store(
    env: str = "development",
    *,
    sqlite_path: str | Path = "./sessions.db",
    postgres_url: str | None = None,
) -> InMemoryKnowledgeGraphStore | SqliteKnowledgeGraphStore | PostgresKnowledgeGraphStore:
    """按运行环境创建知识图谱存储。"""
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return InMemoryKnowledgeGraphStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境知识图谱存储需要 PostgreSQL 连接地址")
        return PostgresKnowledgeGraphStore(postgres_url)
    return SqliteKnowledgeGraphStore(sqlite_path)

