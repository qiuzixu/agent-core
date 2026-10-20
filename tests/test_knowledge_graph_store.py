"""KnowledgeGraphStore 协议与进程内实现测试。"""

import pytest

from agent_core.storage.knowledge_graph import InMemoryKnowledgeGraphStore


async def test_entity_and_relation_round_trip() -> None:
    store = InMemoryKnowledgeGraphStore()
    route = await store.add_entity("route", "R-001", tenant_id="t1")
    vehicle = await store.add_entity("vehicle", "DRONE-001", tenant_id="t1")
    await store.add_relation(
        vehicle["entity_id"], route["entity_id"], "EXECUTES", tenant_id="t1"
    )
    result = await store.query_subgraph(
        route["entity_id"], direction="incoming", tenant_id="t1"
    )
    assert result["start_entity_id"] == "route:R-001"
    assert {item["entity_type"] for item in result["entities"]} == {"route", "vehicle"}
    assert result["relations"][0]["relation_type"] == "EXECUTES"


async def test_query_filters_relation_and_entity_types() -> None:
    store = InMemoryKnowledgeGraphStore()
    route = await store.add_entity("route", "R-001", tenant_id="t1")
    vehicle = await store.add_entity("vehicle", "DRONE-001", tenant_id="t1")
    place = await store.add_entity("place", "广州塔", tenant_id="t1")
    await store.add_relation(vehicle["entity_id"], route["entity_id"], "EXECUTES", tenant_id="t1")
    await store.add_relation(route["entity_id"], place["entity_id"], "HAS_END", tenant_id="t1")
    result = await store.query_subgraph(
        route["entity_id"],
        direction="outgoing",
        relation_types=["HAS_END"],
        entity_types=["place"],
        tenant_id="t1",
    )
    assert [item["entity_type"] for item in result["entities"]] == ["route", "place"]
    assert [item["relation_type"] for item in result["relations"]] == ["HAS_END"]


async def test_relation_requires_existing_entities() -> None:
    store = InMemoryKnowledgeGraphStore()
    with pytest.raises(ValueError):
        await store.add_relation("route:R-404", "vehicle:X", "EXECUTES")