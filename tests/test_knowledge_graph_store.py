"""KnowledgeGraphStore 协议与进程内实现测试。"""

import pytest

from agent_core.storage.knowledge_graph import (
    InMemoryKnowledgeGraphStore,
    SqliteKnowledgeGraphStore,
    create_knowledge_graph_store,
)


async def test_entity_and_relation_round_trip() -> None:
    store = InMemoryKnowledgeGraphStore()
    route = await store.add_entity("route", "R-001", tenant_id="t1")
    vehicle = await store.add_entity("vehicle", "DRONE-001", tenant_id="t1")
    await store.add_relation(
        vehicle.entity_id, route.entity_id, "EXECUTES", tenant_id="t1"
    )
    result = await store.query_subgraph(
        route.entity_id, direction="incoming", tenant_id="t1"
    )
    assert result.start_entity_id == "route:R-001"
    assert {item.entity_type for item in result.entities} == {"route", "vehicle"}
    assert result.relations[0].relation_type == "EXECUTES"


async def test_query_filters_relation_and_entity_types() -> None:
    store = InMemoryKnowledgeGraphStore()
    route = await store.add_entity("route", "R-001", tenant_id="t1")
    vehicle = await store.add_entity("vehicle", "DRONE-001", tenant_id="t1")
    place = await store.add_entity("place", "广州塔", tenant_id="t1")
    await store.add_relation(vehicle.entity_id, route.entity_id, "EXECUTES", tenant_id="t1")
    await store.add_relation(route.entity_id, place.entity_id, "HAS_END", tenant_id="t1")
    result = await store.query_subgraph(
        route.entity_id,
        direction="outgoing",
        relation_types=["HAS_END"],
        entity_types=["place"],
        tenant_id="t1",
    )
    assert [item.entity_type for item in result.entities] == ["route", "place"]
    assert [item.relation_type for item in result.relations] == ["HAS_END"]


async def test_relation_requires_existing_entities() -> None:
    store = InMemoryKnowledgeGraphStore()
    with pytest.raises(ValueError):
        await store.add_relation("route:R-404", "vehicle:X", "EXECUTES")


async def test_sqlite_roundtrip_persists(tmp_path) -> None:
    """SQLite 实现写入后重开实例仍能查询图谱。"""
    database = tmp_path / "knowledge-graph.db"
    store = SqliteKnowledgeGraphStore(database)
    route = await store.add_entity("route", "R-001", tenant_id="t1")
    vehicle = await store.add_entity("vehicle", "DRONE-001", tenant_id="t1")
    await store.add_relation(
        vehicle.entity_id, route.entity_id, "EXECUTES", tenant_id="t1"
    )

    reopened = SqliteKnowledgeGraphStore(database)
    assert await reopened.get_entity(route.entity_id, tenant_id="t1") is not None
    result = await reopened.query_subgraph(
        route.entity_id, direction="incoming", tenant_id="t1"
    )
    assert result.start_entity_id == "route:R-001"
    assert {item.entity_type for item in result.entities} == {"route", "vehicle"}
    assert result.relations[0].relation_type == "EXECUTES"


def test_create_knowledge_graph_store_requires_postgres_url() -> None:
    """生产环境必须显式提供 PostgreSQL DSN。"""
    import pytest

    with pytest.raises(ValueError):
        create_knowledge_graph_store("production")
