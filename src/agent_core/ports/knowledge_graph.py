"""通用知识图谱存储端口。

Agent Core 只关心可信实体、关系和受控子图查询；具体实现可以放在进程内、
SQLite、PostgreSQL 或专用图数据库中。业务实体类型和关系类型由上层应用定义。
"""

from __future__ import annotations

from collections.abc import Iterable
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
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    async def get_entity(
        self,
        entity_id: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None: ...

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
    ) -> dict[str, Any]: ...