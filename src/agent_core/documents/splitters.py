"""文档切分协议和轻量递归字符切分器。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

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

    def split_documents_with_stats(
        self, documents: list[Document]
    ) -> tuple[list[DocumentChunk], SplitStats]:
        """切分并返回统计结果。

        oversized_chunks 达到上限通常意味着语料里缺少分隔符（如整段压缩文本），
        应考虑调整 chunk_size 或补充分隔符，而不是盲目调大 overlap。
        """
        chunks = self.split_documents(documents)
        if chunks:
            lengths = [len(chunk.content) for chunk in chunks]
            total_chars = sum(lengths)
            stats = SplitStats(
                document_count=len(documents),
                chunk_count=len(chunks),
                total_chars=total_chars,
                average_chunk_chars=total_chars / len(chunks),
                max_chunk_chars=max(lengths),
                oversized_chunks=sum(1 for length in lengths if length >= self._chunk_size),
            )
        else:
            stats = SplitStats(
                document_count=len(documents),
                chunk_count=0,
                total_chars=0,
                average_chunk_chars=0.0,
                max_chunk_chars=0,
                oversized_chunks=0,
            )
        return chunks, stats

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


@dataclass(frozen=True)
class SplitStats:
    """切分统计结果，用于入库前评估切分质量。"""

    document_count: int
    chunk_count: int
    total_chars: int
    average_chunk_chars: float
    max_chunk_chars: int
    # 长度达到 chunk_size 上限的片段数（说明内容密集、找不到自然边界被硬切）
    oversized_chunks: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "total_chars": self.total_chars,
            "average_chunk_chars": self.average_chunk_chars,
            "max_chunk_chars": self.max_chunk_chars,
            "oversized_chunks": self.oversized_chunks,
        }
