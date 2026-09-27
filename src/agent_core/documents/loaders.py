"""文档加载和 Blob 解析协议，以及零依赖文本实现。"""

from __future__ import annotations

import asyncio
import mimetypes
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent_core.documents.types import Blob, Document, DocumentLocator


@runtime_checkable
class DocumentLoader(Protocol):
    """把外部资源加载为 Document。"""

    async def load(self) -> list[Document]: ...

    def lazy_load(self) -> AsyncIterator[Document]: ...


@runtime_checkable
class BlobParser(Protocol):
    """把原始 Blob 解析为一个或多个 Document。"""

    async def parse(self, blob: Blob) -> list[Document]: ...


class TextBlobParser:
    """按指定字符集解析纯文本 Blob。"""

    def __init__(self, encoding: str = "utf-8") -> None:
        self._encoding = encoding

    async def parse(self, blob: Blob) -> list[Document]:
        content = blob.data.decode(self._encoding)
        source_key = blob.source or f"blob:{blob.checksum}"
        return [
            Document(
                document_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_key}:{blob.checksum}")),
                content=content,
                source=blob.source,
                mime_type=blob.mime_type or "text/plain",
                metadata={**blob.metadata, "source_checksum": blob.checksum},
                locator=DocumentLocator(uri=blob.source) if blob.source else None,
            )
        ]


class TextLoader:
    """从本地文件加载 UTF 文本，不引入第三方解析库。"""

    def __init__(
        self,
        path: str | Path,
        *,
        encoding: str = "utf-8",
        metadata: dict[str, object] | None = None,
    ) -> None:
        self._path = Path(path)
        self._parser = TextBlobParser(encoding)
        self._metadata = dict(metadata or {})

    async def load(self) -> list[Document]:
        data = await asyncio.to_thread(self._path.read_bytes)
        mime_type, _ = mimetypes.guess_type(self._path.name)
        blob = Blob(
            data=data,
            source=str(self._path.resolve()),
            mime_type=mime_type or "text/plain",
            metadata=self._metadata,
        )
        return await self._parser.parse(blob)

    async def lazy_load(self) -> AsyncIterator[Document]:
        for document in await self.load():
            yield document
