"""仅允许显式注册类型的 JSON 序列化注册表。

本模块不会根据输入中的 Python 模块或类路径执行动态导入，也不使用 pickle。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import hashlib

from agent_core.access import AccessContext
from agent_core.documents import Blob, Document, DocumentChunk, DocumentLocator
from agent_core.errors import (
    SerializationError,
    UnknownSerializedTypeError,
    UnsupportedSchemaVersionError,
)
from agent_core.ports.knowledge_graph import GraphEntity, GraphRelation, GraphSubgraph
from agent_core.ports.model_selection import ModelSelection
from agent_core.ports.platform import AlertPayload, DeadLetterRecord
from agent_core.protocol import ApprovalRecord, Message, RunContext, RunEvent, ToolResult

Encoder = Callable[[Any], dict[str, Any]]
Decoder = Callable[[dict[str, Any]], Any]
Migration = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class SerializedEnvelope:
    """持久化对象的类型和 Schema 版本信封。

    注意区分两种版本号：

    - 本信封的 ``schema_version`` 是**序列化格式版本**，表示 payload 字段结构
      的演进版本，由 :class:`SerializerRegistry` 负责迁移；
    - ``RunEvent`` / ``RunContext`` 等协议对象内部的 ``schema_version``
      是**运行协议版本**，属于 payload 内容的一部分，两者互不影响。
      即使未来协议对象升级到 2，信封格式仍可保持 1。
    """

    type_id: str
    schema_version: int
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.type_id.strip():
            raise SerializationError("type_id 不能为空")
        if self.schema_version <= 0:
            raise SerializationError("schema_version 必须大于 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type_id,
            "schema_version": self.schema_version,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SerializedEnvelope:
        if not isinstance(value.get("payload"), dict):
            raise SerializationError("序列化 payload 必须是对象")
        try:
            return cls(
                type_id=str(value["type"]),
                schema_version=int(value["schema_version"]),
                payload=dict(value["payload"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SerializationError("序列化信封缺少有效的 type 或 schema_version") from exc


@dataclass(frozen=True)
class _Codec:
    type_id: str
    python_type: type[Any]
    schema_version: int
    encoder: Encoder
    decoder: Decoder
    migrations: dict[int, Migration] = field(default_factory=dict)


class SerializerRegistry:
    """显式白名单注册表，负责版本迁移和对象重建。"""

    def __init__(self) -> None:
        self._by_type_id: dict[str, _Codec] = {}
        self._by_python_type: dict[type[Any], _Codec] = {}

    def register(
        self,
        type_id: str,
        python_type: type[Any],
        *,
        schema_version: int,
        encoder: Encoder,
        decoder: Decoder,
        migrations: Mapping[int, Migration] | None = None,
    ) -> None:
        """注册对象编码器；migration[n] 负责把版本 n 升级到 n+1。"""

        normalized = type_id.strip()
        if not normalized:
            raise ValueError("type_id 不能为空")
        if schema_version <= 0:
            raise ValueError("schema_version 必须大于 0")
        if normalized in self._by_type_id:
            raise ValueError(f"序列化类型已注册：{normalized}")
        if python_type in self._by_python_type:
            raise ValueError(f"Python 类型已注册：{python_type.__qualname__}")
        codec = _Codec(
            type_id=normalized,
            python_type=python_type,
            schema_version=schema_version,
            encoder=encoder,
            decoder=decoder,
            migrations=dict(migrations or {}),
        )
        self._by_type_id[normalized] = codec
        self._by_python_type[python_type] = codec

    def dump(self, value: Any) -> SerializedEnvelope:
        codec = self._by_python_type.get(type(value))
        if codec is None:
            raise UnknownSerializedTypeError(f"类型未注册：{type(value).__qualname__}")
        try:
            payload = codec.encoder(value)
        except SerializationError:
            raise
        except Exception as exc:
            raise SerializationError(f"{codec.type_id} 编码失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise SerializationError(f"{codec.type_id} 编码器必须返回 dict")
        return SerializedEnvelope(codec.type_id, codec.schema_version, payload)

    def load(self, value: SerializedEnvelope | Mapping[str, Any]) -> Any:
        envelope = value if isinstance(value, SerializedEnvelope) else SerializedEnvelope.from_dict(value)
        codec = self._by_type_id.get(envelope.type_id)
        if codec is None:
            raise UnknownSerializedTypeError(f"序列化类型未注册：{envelope.type_id}")
        if envelope.schema_version > codec.schema_version:
            raise UnsupportedSchemaVersionError(
                f"{envelope.type_id} 版本 {envelope.schema_version} 高于当前版本 {codec.schema_version}"
            )
        payload = dict(envelope.payload)
        version = envelope.schema_version
        while version < codec.schema_version:
            migration = codec.migrations.get(version)
            if migration is None:
                raise UnsupportedSchemaVersionError(
                    f"{envelope.type_id} 缺少 {version} -> {version + 1} 迁移函数"
                )
            try:
                payload = migration(payload)
            except Exception as exc:
                raise SerializationError(f"{envelope.type_id} 从版本 {version} 迁移失败：{exc}") from exc
            if not isinstance(payload, dict):
                raise SerializationError(
                    f"{envelope.type_id} 的 {version} -> {version + 1} 迁移必须返回 dict"
                )
            version += 1
        try:
            return codec.decoder(payload)
        except SerializationError:
            raise
        except Exception as exc:
            raise SerializationError(f"{envelope.type_id} 解码失败：{exc}") from exc

    def dumps(self, value: Any, *, ensure_ascii: bool = False) -> str:
        try:
            return json.dumps(self.dump(value).to_dict(), ensure_ascii=ensure_ascii)
        except (TypeError, ValueError) as exc:
            raise SerializationError(f"对象不能编码为 JSON：{exc}") from exc

    def loads(self, value: str) -> Any:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SerializationError(f"无效 JSON：{exc}") from exc
        if not isinstance(decoded, dict):
            raise SerializationError("序列化根节点必须是对象")
        return self.load(decoded)


def _encode_blob(value: Blob) -> dict[str, Any]:
    return {
        "data": base64.b64encode(value.data).decode("ascii"),
        "data_encoding": "base64",
        "source": value.source,
        "mime_type": value.mime_type,
        "metadata": dict(value.metadata),
        "checksum": value.checksum,
    }


def _decode_blob(value: dict[str, Any]) -> Blob:
    """从 base64 恢复 Blob，并严格校验 sha256 checksum 防止数据被篡改。"""

    if value.get("data_encoding") != "base64":
        raise SerializationError("Blob 只接受 base64 数据")
    try:
        data = base64.b64decode(str(value.get("data", "")), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SerializationError("Blob 包含无效 base64 数据") from exc
    checksum = str(value.get("checksum", "")).strip().lower()
    if not checksum:
        raise SerializationError("Blob 缺少 checksum，拒绝恢复（防篡改校验必须开启）")
    actual = hashlib.sha256(data).hexdigest()
    if actual != checksum:
        raise SerializationError(
            f"Blob checksum 不匹配：预期 {checksum}，实际 {actual}"
        )
    return Blob(
        data=data,
        source=value.get("source"),
        mime_type=value.get("mime_type"),
        metadata=dict(value.get("metadata") or {}),
        checksum=checksum,
    )


def _decode_message(value: dict[str, Any]) -> Message:
    return Message.from_dict(value)


def _decode_access(value: dict[str, Any]) -> AccessContext:
    """恢复访问上下文；关键字段必须有效，不接受静默降级。"""

    user_id = value.get("user_id")
    tenant_id = value.get("tenant_id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise SerializationError("AccessContext.user_id 必须是非空字符串")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise SerializationError("AccessContext.tenant_id 必须是非空字符串")
    raw_roles = value.get("roles", ["user"])
    if not isinstance(raw_roles, (list, tuple, set, frozenset)):
        raise SerializationError("AccessContext.roles 必须是可迭代集合")
    roles: frozenset[str] = frozenset()
    for role in raw_roles:
        text = str(role).strip()
        if not text:
            raise SerializationError("AccessContext.roles 不能包含空角色")
        roles = frozenset([*roles, text])
    return AccessContext(user_id=user_id, tenant_id=tenant_id, roles=roles)


def _encode_graph_entity(value: GraphEntity) -> dict[str, Any]:
    return value.to_dict()


def _encode_graph_relation(value: GraphRelation) -> dict[str, Any]:
    return value.to_dict()


def _encode_graph_subgraph(value: GraphSubgraph) -> dict[str, Any]:
    return value.to_dict()


def _encode_alert_payload(value: AlertPayload) -> dict[str, Any]:
    return {
        "key": value.key,
        "message": value.message,
        "severity": value.severity,
        "timestamp": value.timestamp,
        "metadata": dict(value.metadata),
    }


def _decode_alert_payload(value: dict[str, Any]) -> AlertPayload:
    return AlertPayload(
        key=str(value["key"]),
        message=str(value["message"]),
        severity=str(value.get("severity", "warning")),
        timestamp=float(value.get("timestamp", 0.0)),
        metadata=dict(value.get("metadata") or {}),
    )


def _encode_dead_letter_record(value: DeadLetterRecord) -> dict[str, Any]:
    return {
        "timestamp": value.timestamp,
        "tool_name": value.tool_name,
        "args_summary": value.args_summary,
        "error": value.error,
        "session_id": value.session_id,
        "user_id": value.user_id,
    }


def _decode_dead_letter_record(value: dict[str, Any]) -> DeadLetterRecord:
    return DeadLetterRecord(
        timestamp=float(value["timestamp"]),
        tool_name=str(value["tool_name"]),
        args_summary=str(value["args_summary"]),
        error=str(value["error"]),
        session_id=str(value.get("session_id", "")),
        user_id=str(value.get("user_id", "")),
    )


def _build_default_registry() -> SerializerRegistry:
    registry = SerializerRegistry()
    registry.register(
        "agent_core.blob",
        Blob,
        schema_version=1,
        encoder=_encode_blob,
        decoder=_decode_blob,
    )
    registry.register(
        "agent_core.document_locator",
        DocumentLocator,
        schema_version=1,
        encoder=DocumentLocator.to_dict,
        decoder=DocumentLocator.from_dict,
    )
    registry.register(
        "agent_core.document",
        Document,
        schema_version=1,
        encoder=Document.to_dict,
        decoder=Document.from_dict,
    )
    registry.register(
        "agent_core.document_chunk",
        DocumentChunk,
        schema_version=1,
        encoder=DocumentChunk.to_dict,
        decoder=DocumentChunk.from_dict,
    )
    registry.register(
        "agent_core.message",
        Message,
        schema_version=1,
        encoder=Message.to_dict,
        decoder=_decode_message,
    )
    registry.register(
        "agent_core.run_event",
        RunEvent,
        schema_version=1,
        encoder=RunEvent.to_dict,
        decoder=RunEvent.from_dict,
    )
    registry.register(
        "agent_core.run_context",
        RunContext,
        schema_version=1,
        encoder=RunContext.to_dict,
        decoder=RunContext.from_dict,
    )
    registry.register(
        "agent_core.approval_record",
        ApprovalRecord,
        schema_version=1,
        encoder=ApprovalRecord.to_dict,
        decoder=ApprovalRecord.from_dict,
    )
    registry.register(
        "agent_core.tool_result",
        ToolResult,
        schema_version=1,
        encoder=ToolResult.to_dict,
        decoder=ToolResult.from_dict,
    )
    registry.register(
        "agent_core.access_context",
        AccessContext,
        schema_version=1,
        encoder=AccessContext.to_dict,
        decoder=_decode_access,
    )
    registry.register(
        "agent_core.graph_entity",
        GraphEntity,
        schema_version=1,
        encoder=_encode_graph_entity,
        decoder=GraphEntity.from_dict,
    )
    registry.register(
        "agent_core.graph_relation",
        GraphRelation,
        schema_version=1,
        encoder=_encode_graph_relation,
        decoder=GraphRelation.from_dict,
    )
    registry.register(
        "agent_core.graph_subgraph",
        GraphSubgraph,
        schema_version=1,
        encoder=_encode_graph_subgraph,
        decoder=GraphSubgraph.from_dict,
    )
    registry.register(
        "agent_core.model_selection",
        ModelSelection,
        schema_version=1,
        encoder=ModelSelection.to_dict,
        decoder=ModelSelection.from_dict,
    )
    registry.register(
        "agent_core.alert_payload",
        AlertPayload,
        schema_version=1,
        encoder=_encode_alert_payload,
        decoder=_decode_alert_payload,
    )
    registry.register(
        "agent_core.dead_letter_record",
        DeadLetterRecord,
        schema_version=1,
        encoder=_encode_dead_letter_record,
        decoder=_decode_dead_letter_record,
    )
    return registry


default_serializer_registry = _build_default_registry()


def dumpd(value: Any, *, registry: SerializerRegistry = default_serializer_registry) -> dict[str, Any]:
    """把对象编码为包含类型和版本的字典。"""

    return registry.dump(value).to_dict()


def dumps(value: Any, *, registry: SerializerRegistry = default_serializer_registry) -> str:
    """把对象安全编码为 JSON。"""

    return registry.dumps(value)


def load(
    value: SerializedEnvelope | Mapping[str, Any],
    *,
    registry: SerializerRegistry = default_serializer_registry,
) -> Any:
    """从版本化信封恢复白名单中的对象。"""

    return registry.load(value)


def loads(value: str, *, registry: SerializerRegistry = default_serializer_registry) -> Any:
    """从 JSON 恢复白名单中的对象。"""

    return registry.loads(value)
