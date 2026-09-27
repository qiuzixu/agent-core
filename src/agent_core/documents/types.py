"""与模型和存储实现无关的文档值对象。"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class DocumentLocator:
    """文档中的稳定位置，用于引用、续读和追踪切分来源。"""

    uri: str | None = None
    page: int | None = None
    block_id: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "page": self.page,
            "block_id": self.block_id,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DocumentLocator:
        return cls(
            uri=value.get("uri"),
            page=int(value["page"]) if value.get("page") is not None else None,
            block_id=value.get("block_id"),
            start_offset=(int(value["start_offset"]) if value.get("start_offset") is not None else None),
            end_offset=int(value["end_offset"]) if value.get("end_offset") is not None else None,
        )


@dataclass(frozen=True)
class Blob:
    """尚未解析的原始二进制内容。"""

    data: bytes
    source: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.checksum:
            object.__setattr__(self, "checksum", _sha256(self.data))

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        source: str | None = None,
        mime_type: str = "text/plain",
        metadata: dict[str, Any] | None = None,
        encoding: str = "utf-8",
    ) -> Blob:
        return cls(
            text.encode(encoding),
            source=source,
            mime_type=mime_type,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Blob 的 JSON 编码由 serialization 模块负责，这里保留原始 bytes。"""

        return {
            "data": self.data,
            "source": self.source,
            "mime_type": self.mime_type,
            "metadata": dict(self.metadata),
            "checksum": self.checksum,
        }


@dataclass(frozen=True)
class Document:
    """解析后的文本及其来源信息。"""

    content: str
    document_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    locator: DocumentLocator | None = None
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.document_id.strip():
            raise ValueError("document_id 不能为空")
        if not self.checksum:
            object.__setattr__(self, "checksum", _sha256(self.content.encode("utf-8")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "content": self.content,
            "source": self.source,
            "mime_type": self.mime_type,
            "metadata": dict(self.metadata),
            "locator": self.locator.to_dict() if self.locator else None,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Document:
        locator = value.get("locator")
        return cls(
            document_id=str(value["document_id"]),
            content=str(value.get("content", "")),
            source=value.get("source"),
            mime_type=value.get("mime_type"),
            metadata=dict(value.get("metadata") or {}),
            locator=DocumentLocator.from_dict(locator) if isinstance(locator, dict) else None,
            checksum=str(value.get("checksum", "")),
        )


@dataclass(frozen=True)
class DocumentChunk:
    """可索引的文档片段，保留父文档和字符区间。"""

    content: str
    parent_document_id: str
    chunk_index: int
    document_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    locator: DocumentLocator | None = None
    checksum: str = ""

    def __post_init__(self) -> None:
        if self.chunk_index < 0:
            raise ValueError("chunk_index 不能小于 0")
        if not self.parent_document_id.strip():
            raise ValueError("parent_document_id 不能为空")
        if not self.document_id.strip():
            raise ValueError("document_id 不能为空")
        if not self.checksum:
            object.__setattr__(self, "checksum", _sha256(self.content.encode("utf-8")))

    def to_document(self) -> Document:
        metadata = {
            **self.metadata,
            "parent_document_id": self.parent_document_id,
            "chunk_index": self.chunk_index,
        }
        return Document(
            document_id=self.document_id,
            content=self.content,
            source=self.source,
            mime_type=self.mime_type,
            metadata=metadata,
            locator=self.locator,
            checksum=self.checksum,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "content": self.content,
            "parent_document_id": self.parent_document_id,
            "chunk_index": self.chunk_index,
            "source": self.source,
            "mime_type": self.mime_type,
            "metadata": dict(self.metadata),
            "locator": self.locator.to_dict() if self.locator else None,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DocumentChunk:
        locator = value.get("locator")
        return cls(
            document_id=str(value["document_id"]),
            content=str(value.get("content", "")),
            parent_document_id=str(value["parent_document_id"]),
            chunk_index=int(value["chunk_index"]),
            source=value.get("source"),
            mime_type=value.get("mime_type"),
            metadata=dict(value.get("metadata") or {}),
            locator=DocumentLocator.from_dict(locator) if isinstance(locator, dict) else None,
            checksum=str(value.get("checksum", "")),
        )
