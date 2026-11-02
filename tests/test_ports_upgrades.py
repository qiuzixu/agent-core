"""ports 本轮契约升级测试。"""

import pytest

from agent_core.middleware.dynamic_config import FileConfigCenter
from agent_core.ports.knowledge_graph import GraphSubgraph
from agent_core.storage.knowledge_graph import InMemoryKnowledgeGraphStore
from agent_core.storage.model_selection import (
    MemoryModelSelectionStore,
    ModelSelectionConcurrencyError,
)


async def test_model_selection_expected_version_matches(tmp_path) -> None:
    store = MemoryModelSelectionStore()
    first = await store.save("openai", "gpt-4o-mini", scope="tenant", user_id=None, tenant_id="t1")
    assert first.version == 1

    second = await store.save(
        "openai",
        "gpt-5-mini",
        scope="tenant",
        user_id=None,
        tenant_id="t1",
        expected_version=first.version,
    )
    assert second.version == 2


async def test_model_selection_expected_version_conflict() -> None:
    store = MemoryModelSelectionStore()
    await store.save("openai", "gpt-4o-mini", scope="tenant", user_id=None, tenant_id="t1")
    with pytest.raises(ModelSelectionConcurrencyError):
        await store.save(
            "openai",
            "gpt-5-mini",
            scope="tenant",
            user_id=None,
            tenant_id="t1",
            expected_version=99,
        )


def test_config_center_on_change_unsubscribe(tmp_path) -> None:
    path = tmp_path / "dynamic.json"
    path.write_text("{}", encoding="utf-8")
    center = FileConfigCenter(str(path), enabled=True)

    changes: list[dict] = []
    unsubscribe = center.on_change(changes.append)
    center.set("enabled", True)
    assert changes == [{"enabled": True}]

    unsubscribe()
    center.set("enabled", False)
    assert changes == [{"enabled": True}]


async def test_knowledge_graph_store_returns_dataclasses() -> None:
    store = InMemoryKnowledgeGraphStore()
    route = await store.add_entity("route", "R-001", tenant_id="t1")
    vehicle = await store.add_entity("vehicle", "DRONE-001", tenant_id="t1")
    relation = await store.add_relation(vehicle.entity_id, route.entity_id, "EXECUTES", tenant_id="t1")
    result = await store.query_subgraph(route.entity_id, direction="incoming", tenant_id="t1")

    assert route.entity_type == "route"
    assert relation.relation_type == "EXECUTES"
    assert isinstance(result, GraphSubgraph)
    assert result.entities[0].entity_id == route.entity_id
