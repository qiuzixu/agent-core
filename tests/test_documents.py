"""documents 模块升级点的回归测试。

覆盖：checksum 防篡改、去重入口、真流式 lazy_load、
metadata 保留键、解析器注册表、切分统计。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_core.documents import (
    Document,
    DocumentChunk,
    DocumentMetadataKeys,
    ParserNotFoundError,
    ParserRegistry,
    RecursiveCharacterTextSplitter,
    TextLoader,
    default_parser_registry,
    deduplicate_chunks,
    deduplicate_documents,
)
from agent_core.documents.types import Blob


# ---------------------------------------------------------------------------
# P1：from_dict / 构造时的 checksum 防篡改校验
# ---------------------------------------------------------------------------


def test_document_from_dict_rejects_tampered_checksum() -> None:
    """checksum 与内容不一致时必须拒绝恢复，防止数据被篡改。"""
    document = Document(content="原始内容")
    payload = document.to_dict()
    payload["content"] = "被篡改的内容"
    with pytest.raises(ValueError, match="checksum"):
        Document.from_dict(payload)


def test_document_from_dict_backfills_missing_checksum() -> None:
    """旧数据没有 checksum 时自动计算，保持向后兼容。"""
    document = Document(content="旧数据")
    payload = document.to_dict()
    payload.pop("checksum")
    restored = Document.from_dict(payload)
    assert restored.checksum == document.checksum


def test_chunk_from_dict_rejects_tampered_checksum() -> None:
    """DocumentChunk 同样执行防篡改校验。"""
    chunk = DocumentChunk(
        content="片段内容",
        parent_document_id="doc-1",
        chunk_index=0,
    )
    payload = chunk.to_dict()
    payload["content"] = "被篡改的片段"
    with pytest.raises(ValueError, match="checksum"):
        DocumentChunk.from_dict(payload)


# ---------------------------------------------------------------------------
# P1：按 checksum 去重的统一入口
# ---------------------------------------------------------------------------


def test_deduplicate_documents_keeps_first_occurrence() -> None:
    """重复文档按 checksum 去重，保留首次出现的对象。"""
    first = Document(content="重复内容")
    second = Document(content="重复内容")
    unique = Document(content="独有内容")
    result = deduplicate_documents([first, second, unique])
    assert len(result) == 2
    assert result[0].document_id == first.document_id
    assert result[1].checksum == unique.checksum


def test_deduplicate_chunks_removes_empty_duplicates() -> None:
    """切分产生的重复片段（如空段）被过滤。"""
    chunks = [
        DocumentChunk(content="片段A", parent_document_id="d", chunk_index=0),
        DocumentChunk(content="", parent_document_id="d", chunk_index=1),
        DocumentChunk(content="", parent_document_id="d", chunk_index=2),
    ]
    result = deduplicate_chunks(chunks)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# P2：真流式 lazy_load
# ---------------------------------------------------------------------------


def _write_lines(tmp_path: Path, line_count: int, line_chars: int = 10) -> Path:
    """生成多行文本文件，每行固定长度并以换行结尾。"""
    path = tmp_path / "stream.txt"
    content = "".join(f"{i:08d}xx\n"[:line_chars] for i in range(line_count))
    path.write_text(content, encoding="utf-8")
    return path


def test_lazy_load_streams_in_batches(tmp_path: Path) -> None:
    """大文件按 batch_chars 分批产出，各批区间连续且拼接还原全文。"""
    path = _write_lines(tmp_path, line_count=500)
    full_text = path.read_text(encoding="utf-8")

    async def run() -> list[Document]:
        loader = TextLoader(path, batch_chars=200)
        return [doc async for doc in loader.lazy_load()]

    documents = asyncio.run(run())
    assert len(documents) > 1
    # 区间必须连续无缝
    expected_start = 0
    for document in documents:
        locator = document.locator
        assert locator is not None
        assert locator.start_offset == expected_start
        expected_start = locator.end_offset
    # 拼接所有批次应完整还原原文
    assert "".join(doc.content for doc in documents) == full_text
    # 批序号从 0 连续递增
    assert [doc.metadata[DocumentMetadataKeys.PART_INDEX] for doc in documents] == list(
        range(len(documents))
    )


def test_lazy_load_deterministic_ids(tmp_path: Path) -> None:
    """同一文件重复流式加载，批 ID 幂等一致。"""
    path = _write_lines(tmp_path, line_count=100)

    async def run() -> tuple[list[str], list[str]]:
        loader = TextLoader(path, batch_chars=150)
        first = [doc.document_id async for doc in loader.lazy_load()]
        second = [doc.document_id async for doc in loader.lazy_load()]
        return first, second

    first_ids, second_ids = asyncio.run(run())
    assert first_ids == second_ids


def test_lazy_load_preserves_multibyte_characters(tmp_path: Path) -> None:
    """多字节中文字符跨读取边界时不得乱码。"""
    path = tmp_path / "chinese.txt"
    path.write_text("低空无人机航线检查。问题一：答案是A。解析：因为..." * 100, encoding="utf-8")

    async def run() -> str:
        loader = TextLoader(path, batch_chars=64)
        documents = [doc async for doc in loader.lazy_load()]
        return "".join(doc.content for doc in documents)

    original = path.read_text(encoding="utf-8")
    assert asyncio.run(run()) == original


# ---------------------------------------------------------------------------
# P2：metadata 保留键 schema
# ---------------------------------------------------------------------------


def test_loader_stamps_canonical_metadata(tmp_path: Path) -> None:
    """Loader 产出的 Document 携带约定的 metadata 保留键。"""
    path = _write_lines(tmp_path, line_count=5)

    async def run() -> Document:
        return (await TextLoader(path).load())[0]

    document = asyncio.run(run())
    assert DocumentMetadataKeys.SOURCE_CHECKSUM in document.metadata
    assert document.metadata[DocumentMetadataKeys.LOADER] == "text_blob_parser"
    assert document.metadata[DocumentMetadataKeys.ENCODING] == "utf-8"


# ---------------------------------------------------------------------------
# P2：解析器注册表
# ---------------------------------------------------------------------------


def test_default_registry_resolves_text_by_mime_and_extension() -> None:
    """默认注册表能按 MIME 通配与扩展名解析文本类 Blob。"""
    assert default_parser_registry.resolve(mime_type="text/plain") is not None
    assert default_parser_registry.resolve(mime_type="text/csv") is not None
    assert default_parser_registry.resolve(mime_type="application/json") is not None
    assert default_parser_registry.resolve(filename="notes.md") is not None
    # 未注册的类型返回 None
    assert default_parser_registry.resolve(mime_type="application/pdf") is None


def test_registry_parses_blob_and_raises_when_missing() -> None:
    """parse_blob 走注册解析；未注册类型抛 ParserNotFoundError。"""
    blob = Blob.from_text("你好", source="a.txt")
    documents = asyncio.run(default_parser_registry.parse_blob(blob))
    assert documents[0].content == "你好"

    unknown = Blob(data=b"%PDF-1.4", source="a.pdf", mime_type="application/pdf")
    with pytest.raises(ParserNotFoundError):
        asyncio.run(default_parser_registry.parse_blob(unknown))


def test_registry_register_custom_parser() -> None:
    """业务层可注册自定义解析器（如 MinerU 接 PDF）。"""
    registry = ParserRegistry()

    class StubParser:
        async def parse(self, blob: Blob) -> list[Document]:
            return [Document(content="stub", source=blob.source)]

    registry.register(
        StubParser(),  # type: ignore[arg-type]
        mime_types=("application/pdf",),
        extensions=(".pdf",),
    )
    assert registry.resolve(mime_type="application/pdf") is not None
    assert registry.resolve(filename="file.pdf") is not None


# ---------------------------------------------------------------------------
# P3：切分统计
# ---------------------------------------------------------------------------


def test_split_documents_with_stats(tmp_path: Path) -> None:
    """统计输出 chunk 数、平均长度、最大长度和硬切数量。"""
    path = tmp_path / "stats.txt"
    path.write_text("段落一。\n\n段落二。\n\n段落三。" * 30, encoding="utf-8")

    async def run() -> Document:
        return (await TextLoader(path).load())[0]

    document = asyncio.run(run())
    splitter = RecursiveCharacterTextSplitter(chunk_size=50, chunk_overlap=5)
    chunks, stats = splitter.split_documents_with_stats([document])
    assert stats.chunk_count == len(chunks)
    assert stats.document_count == 1
    assert stats.total_chars == sum(len(chunk.content) for chunk in chunks)
    assert stats.average_chunk_chars > 0
    assert stats.max_chunk_chars <= 50
    assert stats.to_dict()["chunk_count"] == stats.chunk_count


def test_split_documents_with_stats_empty_input() -> None:
    """空输入返回零值统计，不抛异常。"""
    splitter = RecursiveCharacterTextSplitter()
    chunks, stats = splitter.split_documents_with_stats([])
    assert chunks == []
    assert stats.chunk_count == 0
    assert stats.average_chunk_chars == 0.0

