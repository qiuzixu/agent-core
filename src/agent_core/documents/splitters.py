"""文档切分协议和轻量递归字符切分器。"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from agent_core.documents.types import Document, DocumentChunk, DocumentLocator


@runtime_checkable
class TextSplitter(Protocol):
    """文本和文档切分协议。"""

    def split_text(self, text: str) -> list[str]: ...

    def split_documents(self, documents: list[Document]) -> list[DocumentChunk]: ...


class RecursiveCharacterTextSplitter:
    """按段落、换行和标点优先寻找边界的零依赖切分器。"""

    def __init__(
        self,
        *,
        chunk_size: int = 1000,
        chunk_overlap: int = 100,
        separators: tuple[str, ...] = ("\n\n", "\n", "。", "；", ". ", " "),
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap 必须大于等于 0 且小于 chunk_size")
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._separators = tuple(separator for separator in separators if separator)

    def split_text(self, text: str) -> list[str]:
        return [text[start:end] for start, end in self._split_offsets(text)]

    def split_documents(self, documents: list[Document]) -> list[DocumentChunk]:
        chunks: list[DocumentChunk] = []
        for document in documents:
            base_offset = document.locator.start_offset if document.locator else 0
            for chunk_index, (start, end) in enumerate(self._split_offsets(document.content)):
                locator = DocumentLocator(
                    uri=document.locator.uri if document.locator else document.source,
                    page=document.locator.page if document.locator else None,
                    block_id=document.locator.block_id if document.locator else None,
                    start_offset=(base_offset or 0) + start,
                    end_offset=(base_offset or 0) + end,
                )
                chunks.append(
                    DocumentChunk(
                        document_id=str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"{document.document_id}:{chunk_index}:{start}:{end}",
                            )
                        ),
                        content=document.content[start:end],
                        parent_document_id=document.document_id,
                        chunk_index=chunk_index,
                        source=document.source,
                        mime_type=document.mime_type,
                        metadata=dict(document.metadata),
                        locator=locator,
                    )
                )
        return chunks

    def _split_offsets(self, text: str) -> list[tuple[int, int]]:
        if not text:
            return []
        offsets: list[tuple[int, int]] = []
        start = 0
        length = len(text)
        while start < length:
            hard_end = min(start + self._chunk_size, length)
            end = hard_end
            if hard_end < length:
                minimum = start + max(1, self._chunk_size // 2)
                for separator in self._separators:
                    position = text.rfind(separator, minimum, hard_end)
                    if position >= minimum:
                        end = position + len(separator)
                        break
            if end <= start:
                end = hard_end
            offsets.append((start, end))
            if end >= length:
                break
            start = max(start + 1, end - self._chunk_overlap)
        return offsets
