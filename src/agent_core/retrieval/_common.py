"""向量存储适配器共享的序列化、权限和向量校验。"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

from agent_core.access import AccessContext
from agent_core.documents import Document
from agent_core.retrieval.types import VectorRecord


def normalize_record(
    record: VectorRecord,
    access: AccessContext | None,
    *,
    require_access: bool,
) -> VectorRecord:
    """验证写入身份，并为未声明归属的新记录补齐用户和租户。"""
    if require_access and access is None:
        raise PermissionError("该 VectorStore 要求提供 AccessContext")
    if access is None:
        return record
    if not access.can_access(record.user_id, record.tenant_id):
        raise PermissionError(f"无权写入向量记录：{record.record_id}")
    return replace(
        record,
        user_id=record.user_id or access.user_id,
        tenant_id=record.tenant_id or access.tenant_id,
    )


def check_record_access(record: VectorRecord, access: AccessContext | None) -> None:
    """拒绝覆盖或删除当前身份不可访问的既有记录。"""
    if access is not None and not access.can_access(record.user_id, record.tenant_id):
        raise PermissionError(f"无权访问向量记录：{record.record_id}")


def record_accessible(record: VectorRecord, access: AccessContext | None) -> bool:
    return access is None or access.can_access(record.user_id, record.tenant_id)


def metadata_matches(metadata: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(metadata.get(key) == value for key, value in expected.items())


def validate_vector(vector: tuple[float, ...], *, dimension: int | None = None) -> None:
    if not vector:
        raise ValueError("vector 不能为空")
    if dimension is not None and len(vector) != dimension:
        raise ValueError(f"向量维度不一致：期望 {dimension}，实际 {len(vector)}")
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("vector 只能包含有限数值")


def vector_literal(vector: tuple[float, ...]) -> str:
    """生成只包含已校验浮点数的 pgvector 文本表示。"""
    validate_vector(vector)
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def parse_vector(value: object) -> tuple[float, ...]:
    if isinstance(value, str):
        raw = json.loads(value)
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raise ValueError("无法解析向量存储返回的 embedding")
    if not isinstance(raw, (list, tuple)):
        raise ValueError("embedding 必须是数组")
    vector = tuple(float(item) for item in raw)
    validate_vector(vector)
    return vector


def encode_record_payload(record: VectorRecord) -> str:
    """把文档和适配器 metadata 编码成可跨后端保存的 JSON。"""
    try:
        return json.dumps(
            {
                "document": record.document.to_dict(),
                "metadata": record.metadata,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"向量记录 {record.record_id} 包含不可 JSON 序列化的 metadata") from exc


def decode_record_payload(
    payload: object,
    *,
    record_id: str,
    vector: tuple[float, ...],
    namespace: str,
    user_id: str | None,
    tenant_id: str | None,
) -> VectorRecord:
    if isinstance(payload, str):
        raw = json.loads(payload)
    elif isinstance(payload, dict):
        raw = payload
    else:
        raise ValueError(f"向量记录 {record_id} 的 payload 格式无效")
    if not isinstance(raw, dict) or not isinstance(raw.get("document"), dict):
        raise ValueError(f"向量记录 {record_id} 缺少 document payload")
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"向量记录 {record_id} 的 metadata 必须是对象")
    return VectorRecord(
        record_id=record_id,
        vector=vector,
        document=Document.from_dict(raw["document"]),
        namespace=namespace,
        user_id=user_id,
        tenant_id=tenant_id,
        metadata=dict(metadata),
    )
