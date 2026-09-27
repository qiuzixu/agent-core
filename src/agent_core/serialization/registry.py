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

from agent_core.access import AccessContext
from agent_core.documents import Blob, Document, DocumentChunk, DocumentLocator
from agent_core.errors import (
    SerializationError,
    UnknownSerializedTypeError,
    UnsupportedSchemaVersionError,
)
from agent_core.protocol import ApprovalRecord, Message, RunContext, RunEvent, ToolResult

Encoder = Callable[[Any], dict[str, Any]]
Decoder = Callable[[dict[str, Any]], Any]
Migration = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class SerializedEnvelope:
    """持久化对象的类型和 Schema 版本信封。"""

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
                raise SerializationError(
                    f"{envelope.type_id} 从版本 {version} 迁移失败：{exc}"
                ) from exc
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
    if value.get("data_encoding") != "base64":
        raise SerializationError("Blob 只接受 base64 数据")
    try:
        data = base64.b64decode(str(value.get("data", "")), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SerializationError("Blob 包含无效 base64 数据") from exc
    return Blob(
        data=data,
        source=value.get("source"),
        mime_type=value.get("mime_type"),
        metadata=dict(value.get("metadata") or {}),
        checksum=str(value.get("checksum", "")),
    )


def _decode_message(value: dict[str, Any]) -> Message:
    return Message.from_dict(value)


def _decode_access(value: dict[str, Any]) -> AccessContext:
    return AccessContext(
        user_id=str(value.get("user_id", "anonymous")),
        tenant_id=str(value.get("tenant_id", "default")),
        roles=frozenset(str(role) for role in value.get("roles", ["user"])),
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
