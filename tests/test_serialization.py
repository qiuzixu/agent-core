"""序列化模块回归测试：默认白名单 round-trip、校验、迁移。"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from agent_core import (
    AccessContext,
    Blob,
    SerializedEnvelope,
    SerializerRegistry,
    UnknownSerializedTypeError,
    UnsupportedSchemaVersionError,
    dumps,
    loads,
)
from agent_core.ports.knowledge_graph import GraphEntity, GraphRelation, GraphSubgraph
from agent_core.ports.model_selection import ModelSelection
from agent_core.ports.platform import AlertPayload, DeadLetterRecord


class DefaultRegistryTests(unittest.TestCase):
    """默认注册表 round-trip 与新增 codec 测试。"""

    def test_blob_checksum_tamper_detected(self) -> None:
        blob = Blob.from_text("受保护内容", source="memory://safe")
        loaded = loads(dumps(blob))
        self.assertEqual(loaded.data, blob.data)

        envelope = dumps(blob)
        # 篡改 payload 后应抛错
        import json

        decoded = json.loads(envelope)
        decoded["payload"]["data"] = "dGFtcGVyZWQ="
        with self.assertRaises(Exception) as ctx:
            loads(json.dumps(decoded))
        self.assertIn("checksum", str(ctx.exception))

    def test_blob_missing_checksum_rejected(self) -> None:
        import json

        blob = Blob.from_text("无校验内容", source="memory://legacy")
        decoded = json.loads(dumps(blob))
        del decoded["payload"]["checksum"]
        with self.assertRaises(Exception) as ctx:
            loads(json.dumps(decoded))
        self.assertIn("checksum", str(ctx.exception))

    def test_access_context_round_trip(self) -> None:
        access = AccessContext(user_id="alice", tenant_id="tenant-a", roles=frozenset({"admin", "user"}))
        loaded = loads(dumps(access))
        self.assertEqual(loaded.user_id, "alice")
        self.assertEqual(loaded.tenant_id, "tenant-a")
        self.assertEqual(loaded.roles, frozenset({"admin", "user"}))

    def test_access_context_empty_user_rejected(self) -> None:
        import json

        decoded = json.loads(dumps(AccessContext(user_id="bob", tenant_id="t")))
        decoded["payload"]["user_id"] = ""
        with self.assertRaises(Exception):
            loads(json.dumps(decoded))

        decoded2 = json.loads(dumps(AccessContext(user_id="bob", tenant_id="t")))
        decoded2["payload"]["roles"] = "admin"
        with self.assertRaises(Exception):
            loads(json.dumps(decoded2))

    def test_graph_objects_round_trip(self) -> None:
        entity = GraphEntity(entity_id="e1", entity_type="airport", value="虹桥机场")
        relation = GraphRelation(
            relation_id="r1",
            source_entity_id="e1",
            target_entity_id="e2",
            relation_type="connected_to",
        )
        subgraph = GraphSubgraph(start_entity_id="e1", entities=[entity], relations=[relation], hops=2)

        loaded_entity = loads(dumps(entity))
        loaded_relation = loads(dumps(relation))
        loaded_subgraph = loads(dumps(subgraph))

        # GraphEntity.to_dict 有 display_name 兜底语义，用 to_dict 比较字段一致性
        self.assertEqual(loaded_entity.to_dict(), entity.to_dict())
        self.assertEqual(loaded_relation, relation)
        self.assertEqual(loaded_subgraph.start_entity_id, "e1")
        self.assertEqual(loaded_subgraph.entities[0].to_dict(), entity.to_dict())
        self.assertEqual(loaded_subgraph.relations[0], relation)
        self.assertEqual(loaded_subgraph.hops, 2)

    def test_model_selection_round_trip(self) -> None:
        selection = ModelSelection(provider="deepseek", model="deepseek-chat", scope="user", user_id="alice")
        loaded = loads(dumps(selection))
        self.assertEqual(loaded, selection)

    def test_model_selection_invalid_rejected(self) -> None:
        registry = SerializerRegistry()
        registry.register(
            "test.model_selection",
            ModelSelection,
            schema_version=1,
            encoder=ModelSelection.to_dict,
            decoder=ModelSelection.from_dict,
        )
        with self.assertRaises(Exception):
            registry.load(SerializedEnvelope("test.model_selection", 1, {"provider": "", "model": "m", "scope": "user"}))
        with self.assertRaises(Exception):
            registry.load(SerializedEnvelope("test.model_selection", 1, {"provider": "p", "model": "m", "scope": "invalid"}))

    def test_alert_payload_round_trip(self) -> None:
        alert = AlertPayload(key="budget.exceeded", message="预算超限", severity="error", timestamp=123.45, metadata={"x": "1"})
        loaded = loads(dumps(alert))
        self.assertEqual(loaded.key, alert.key)
        self.assertEqual(loaded.message, alert.message)
        self.assertEqual(loaded.severity, alert.severity)
        self.assertEqual(loaded.timestamp, 123.45)
        self.assertEqual(loaded.metadata, {"x": "1"})

    def test_dead_letter_record_round_trip(self) -> None:
        record = DeadLetterRecord(timestamp=1.0, tool_name="flight_query", args_summary="{...}", error="timeout", session_id="s1", user_id="u1")
        loaded = loads(dumps(record))
        self.assertEqual(loaded.tool_name, "flight_query")
        self.assertEqual(loaded.error, "timeout")
        self.assertEqual(loaded.session_id, "s1")
        self.assertEqual(loaded.user_id, "u1")


class MigrationTests(unittest.TestCase):
    """版本迁移机制测试。"""

    def test_migration_chain_applies(self) -> None:
        @dataclass(frozen=True)
        class Example:
            name: str
            enabled: bool
            priority: int

        registry = SerializerRegistry()
        registry.register(
            "test.example_v3",
            Example,
            schema_version=3,
            encoder=lambda value: {"name": value.name, "enabled": value.enabled, "priority": value.priority},
            decoder=lambda value: Example(str(value["name"]), bool(value["enabled"]), int(value["priority"])),
            migrations={
                1: lambda value: {**value, "enabled": True},
                2: lambda value: {**value, "priority": 0},
            },
        )

        loaded = registry.load(SerializedEnvelope("test.example_v3", 1, {"name": "旧数据"}))
        self.assertEqual(loaded, Example("旧数据", True, 0))

    def test_missing_migration_raises(self) -> None:
        @dataclass(frozen=True)
        class Legacy:
            name: str

        registry = SerializerRegistry()
        registry.register(
            "test.legacy",
            Legacy,
            schema_version=2,
            encoder=lambda value: {"name": value.name},
            decoder=lambda value: Legacy(str(value["name"])),
        )

        with self.assertRaises(UnsupportedSchemaVersionError):
            registry.load(SerializedEnvelope("test.legacy", 1, {"name": "旧"}))

    def test_unknown_type_still_rejected(self) -> None:
        registry = SerializerRegistry()
        with self.assertRaises(UnknownSerializedTypeError):
            registry.load({"type": "evil.module", "schema_version": 1, "payload": {}})


if __name__ == "__main__":
    unittest.main()
