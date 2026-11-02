"""按 checksum 的文档去重统一入口。

Blob / Document / DocumentChunk 构造时都会自动计算 sha256 checksum，
因此"内容相同"等价于"checksum 相同"。本模块提供顺序保持的去重函数，
保留首次出现的对象，用于入库前的统一清洗。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from agent_core.documents.types import Document, DocumentChunk

# 值约束型 TypeVar：两种目标类型都有 checksum 字段，函数内部无需窄化
_T = TypeVar("_T", Document, DocumentChunk)


def _deduplicate(items: Iterable[_T]) -> list[_T]:
    """按 checksum 顺序保持去重的内部实现。"""
    seen: set[str] = set()
    result: list[_T] = []
    for item in items:
        if item.checksum in seen:
            continue
        seen.add(item.checksum)
        result.append(item)
    return result


def deduplicate_documents(documents: Iterable[Document]) -> list[Document]:
    """按内容 checksum 去重文档，保留首次出现的对象。

    典型场景：同一文件被多个 Loader 重复加载后入库前清洗。
    """
    return _deduplicate(documents)


def deduplicate_chunks(chunks: Iterable[DocumentChunk]) -> list[DocumentChunk]:
    """按内容 checksum 去重片段，保留首次出现的对象。

    典型场景：chunk_overlap 会产生内容重叠的片段边缘，
    完全相同的片段（例如空段）在这里被过滤。
    """
    return _deduplicate(chunks)
