"""通用知识图谱存储端口。

Agent Core 只关心可信实体、关系和受控子图查询；具体实现可以放在进程内、
SQLite、PostgreSQL 或专用图数据库中。业务实体类型和关系类型由上层应用定义。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

Direction = Literal["outgoing", "incoming", "both"]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class GraphEntity:
    """图谱中的一个可信实体。"""

    entity_id: str
    entity_type: str
    value: str
    display_name: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "value": self.value,
            "display_name": self.display_name or self.value,
            "properties": dict(self.properties),
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GraphEntity:
        """从字典恢复实体对象。"""

        return cls(
            entity_id=str(value["entity_id"]),
            entity_type=str(value["entity_type"]),
            value=str(value["value"]),
            display_name=str(value.get("display_name", "")),
            properties=dict(value.get("properties") or {}),
            source=str(value.get("source", "")),
            created_at=str(value.get("created_at", "")),
            updated_at=str(value.get("updated_at", "")),
        )


@dataclass
class GraphRelation:
    """两个实体之间的有向关系边。"""

    relation_id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str
    properties: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "source_entity_id": self.source_entity_id,
            "target_entity_id": self.target_entity_id,
            "relation_type": self.relation_type,
            "properties": dict(self.properties),
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GraphRelation:
        """从字典恢复关系对象。"""

        return cls(
            relation_id=str(value["relation_id"]),
            source_entity_id=str(value["source_entity_id"]),
            target_entity_id=str(value["target_entity_id"]),
            relation_type=str(value["relation_type"]),
            properties=dict(value.get("properties") or {}),
            source=str(value.get("source", "")),
            created_at=str(value.get("created_at", "")),
            updated_at=str(value.get("updated_at", "")),
        )


@dataclass
class GraphSubgraph:
    """受控子图查询结果。

    端口层统一返回结构化对象；上层 HTTP/API 需要序列化时自行调用
    ``to_dict()`` 或转换成 JSON-safe dict。
    """

    start_entity_id: str
    entities: list[GraphEntity] = field(default_factory=list)
    relations: list[GraphRelation] = field(default_factory=list)
    hops: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_entity_id": self.start_entity_id,
            "entities": [item.to_dict() for item in self.entities],
            "relations": [item.to_dict() for item in self.relations],
            "hops": self.hops,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GraphSubgraph:
        """从字典恢复子图对象。"""

        return cls(
            start_entity_id=str(value["start_entity_id"]),
            entities=[GraphEntity.from_dict(item) for item in value.get("entities") or []],
            relations=[GraphRelation.from_dict(item) for item in value.get("relations") or []],
            hops=int(value.get("hops", 0)),
        )


@runtime_checkable
class KnowledgeGraphStore(Protocol):
    """轻量知识图谱存储协议。

    实现应至少支持租户作用域、实体幂等写入、有向关系写入和受控子图查询。
    ``thread_id`` 用于会话追溯，不改变实体本身的租户级标识。
    """

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
    ) -> GraphEntity: ...

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
    ) -> GraphRelation: ...

    async def get_entity(
        self,
        entity_id: str,
        *,
        tenant_id: str | None = None,
    ) -> GraphEntity | None: ...

    async def search_entities(
        self,
        query: str,
        *,
        limit: int = 20,
        tenant_id: str | None = None,
    ) -> list[GraphEntity]: ...

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
    ) -> GraphSubgraph: ...
