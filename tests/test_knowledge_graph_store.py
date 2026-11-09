"""KnowledgeGraphStore 协议与各后端实现测试。"""

import os

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


async def test_search_entities_matches_id_value_and_display_name() -> None:
    """词面检索应覆盖实体 ID、值与展示名，并严格按租户隔离。"""
    store = InMemoryKnowledgeGraphStore()
    await store.add_entity("place", "广州塔", display_name="广州塔景区", tenant_id="t1")
    await store.add_entity("place", "珠江新城", tenant_id="t1")
    await store.add_entity("place", "广州塔", tenant_id="t2")

    hits = await store.search_entities("广州塔", tenant_id="t1")
    assert [item.value for item in hits] == ["广州塔"]
    assert await store.search_entities("   ", tenant_id="t1") == []
    assert [item.value for item in await store.search_entities("广州", limit=1, tenant_id="t2")] == [
        "广州塔"
    ]
    # 反向包含：整句里出现实体时也应命中。
    assert [
        item.value for item in await store.search_entities("广州塔附近有什么", tenant_id="t1")
    ] == ["广州塔"]


async def test_sqlite_search_entities_survives_reopen(tmp_path) -> None:
    """SQLite 后端重开后仍可按展示名检索到实体。"""
    database = tmp_path / "knowledge-graph.db"
    store = SqliteKnowledgeGraphStore(database)
    await store.add_entity("place", "广州塔", display_name="广州塔景区", tenant_id="t1")
    await store.add_entity("place", "珠江新城", tenant_id="t1")

    reopened = SqliteKnowledgeGraphStore(database)
    hits = await reopened.search_entities("塔", tenant_id="t1")
    assert [item.display_name for item in hits] == ["广州塔景区"]


@pytest.mark.skipif(
    not os.getenv("AGENT_TEST_POSTGRES_URL"),
    reason="未提供 AGENT_TEST_POSTGRES_URL，跳过 PostgreSQL 集成测试",
)
async def test_postgres_search_entities() -> None:
    """PostgreSQL 后端集成测试，仅在显式提供测试库连接时执行。"""
    from agent_core.storage.knowledge_graph import PostgresKnowledgeGraphStore

    store = PostgresKnowledgeGraphStore(os.environ["AGENT_TEST_POSTGRES_URL"])
    await store.initialize()
    try:
        await store.add_entity("place", "广州塔", display_name="广州塔景区", tenant_id="t-search")
        hits = await store.search_entities("广州塔", tenant_id="t-search")
        assert [item.value for item in hits] == ["广州塔"]
    finally:
        await store.close()
