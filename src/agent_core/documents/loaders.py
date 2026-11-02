"""文档加载和 Blob 解析协议，以及零依赖文本实现。"""

from __future__ import annotations

import asyncio
import codecs
import mimetypes
import uuid
from collections.abc import AsyncIterator
from typing import BinaryIO
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent_core.documents.metadata import DocumentMetadataKeys
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
    """按指定字符集解析纯文本 Blob。

    解析结果会写入约定的 metadata 保留键（来源 checksum / 加载器 / 字符集），
    保证下游按 schema 读取而不是猜字符串。
    """

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
                metadata={
                    **blob.metadata,
                    DocumentMetadataKeys.SOURCE_CHECKSUM: blob.checksum,
                    DocumentMetadataKeys.LOADER: "text_blob_parser",
                    DocumentMetadataKeys.ENCODING: self._encoding,
                },
                locator=DocumentLocator(uri=blob.source) if blob.source else None,
            )
        ]


@dataclass(frozen=True)
class _TextBatch:
    """流式读取产出的一批文本及其在全文中的字符区间。"""

    content: str
    start_offset: int
    end_offset: int


class _StreamingTextReader:
    """有状态的流式文本读取器。

    维护文件句柄、增量解码器和跨批余文，保证：
    - 多字节字符跨读取边界不乱码（增量解码）；
    - 优先在换行处切批，单行超长时退化为按上限硬切；
    - 字符偏移量在全文范围内连续，可写入 locator 回溯原始位置。
    """

    _READ_CHUNK_BYTES = 65536

    def __init__(self, path: Path, encoding: str, batch_chars: int) -> None:
        self._path = path
        self._encoding = encoding
        self._batch_chars = batch_chars
        self._handle: BinaryIO | None = None
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        self._pending = ""
        self._char_offset = 0
        self._eof = False

    def open(self) -> None:
        self._handle = self._path.open("rb")

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _require_handle(self) -> BinaryIO:
        """取文件句柄；未 open 就读取属于编程错误，直接抛出。"""
        if self._handle is None:
            raise RuntimeError("流式读取器尚未打开，请先调用 open()")
        return self._handle

    def read_batch(self) -> _TextBatch | None:
        """阻塞式读取下一批；文件读完且无剩余时返回 None。"""
        handle = self._require_handle()
        # 累积文本直到达到单批上限；EOF 时冲刷增量解码器的残余字节
        while len(self._pending) < self._batch_chars and not self._eof:
            chunk = handle.read(self._READ_CHUNK_BYTES)
            if not chunk:
                self._pending += self._decoder.decode(b"", final=True)
                self._eof = True
                break
            self._pending += self._decoder.decode(chunk)
        if not self._pending:
            return None
        if len(self._pending) > self._batch_chars:
            # 优先在批内最后一个换行处切分，避免把一行劈成两半
            cut = self._pending.rfind("\n", 0, self._batch_chars)
            if cut > 0:
                content, self._pending = self._pending[: cut + 1], self._pending[cut + 1 :]
            else:
                # 整批没有任何换行（如压缩后的单行文本），退化为按上限硬切
                content, self._pending = (
                    self._pending[: self._batch_chars],
                    self._pending[self._batch_chars :],
                )
        else:
            content, self._pending = self._pending, ""
        batch = _TextBatch(
            content=content,
            start_offset=self._char_offset,
            end_offset=self._char_offset + len(content),
        )
        self._char_offset += len(content)
        return batch


class TextLoader:
    """从本地文件加载 UTF 文本，不引入第三方解析库。

    - ``load()``：整文件读入，返回单个 Document（与历史行为一致）；
    - ``lazy_load()``：真流式，按 ``batch_chars`` 分批读取并逐批产出 Document，
      单批内存占用有上限，适合大文件。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        encoding: str = "utf-8",
        metadata: dict[str, object] | None = None,
        batch_chars: int = 65536,
    ) -> None:
        self._path = Path(path)
        self._encoding = encoding
        self._parser = TextBlobParser(encoding)
        self._metadata = dict(metadata or {})
        # 下限 1024，防止误配置成极小值导致每批只有几个字符
        self._batch_chars = max(1024, batch_chars)

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
        """真流式加载：逐批读取文件并逐批产出 Document。

        每批是一个 Document，带有全文连续字符区间的 locator 和 part_index；
        批 ID 由（文件路径 + 批序号）确定性生成，重复加载结果幂等。
        """
        source = str(self._path.resolve())
        mime_type, _ = mimetypes.guess_type(self._path.name)
        reader = _StreamingTextReader(self._path, self._encoding, self._batch_chars)
        await asyncio.to_thread(reader.open)
        try:
            batch_index = 0
            while True:
                # 阻塞读放在线程池，避免卡住事件循环
                batch = await asyncio.to_thread(reader.read_batch)
                if batch is None:
                    break
                yield Document(
                    document_id=str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:part:{batch_index}")
                    ),
                    content=batch.content,
                    source=source,
                    mime_type=mime_type or "text/plain",
                    metadata={
                        **self._metadata,
                        DocumentMetadataKeys.LOADER: "text_blob_parser",
                        DocumentMetadataKeys.ENCODING: self._encoding,
                        DocumentMetadataKeys.PART_INDEX: batch_index,
                    },
                    locator=DocumentLocator(
                        uri=source,
                        start_offset=batch.start_offset,
                        end_offset=batch.end_offset,
                    ),
                )
                batch_index += 1
        finally:
            await asyncio.to_thread(reader.close)

