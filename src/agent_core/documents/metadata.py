"""文档 metadata 的最小 schema 约定。

Core 不强制校验业务自由字段，但约定以下**保留键**：
Loader / 切分器写入时必须使用这里的常量，下游消费方按常量读取，
避免各实现各写一套字符串导致 schema 漂移。
"""

from __future__ import annotations


class DocumentMetadataKeys:
    """Document / DocumentChunk metadata 的保留键名。"""

    # 原始来源内容的 sha256（来自 Blob.checksum）
    SOURCE_CHECKSUM = "source_checksum"
    # 产出该对象的加载器/解析器标识，如 "text_blob_parser"
    LOADER = "loader"
    # 文本解码字符集
    ENCODING = "encoding"
    # 流式加载时的分片序号（从 0 开始）
    PART_INDEX = "part_index"
    # DocumentChunk 还原为 Document 时写入的父文档 ID
    PARENT_DOCUMENT_ID = "parent_document_id"
    # DocumentChunk 还原为 Document 时写入的片段序号
    CHUNK_INDEX = "chunk_index"


#: 全部保留键的只读集合，供下游做白名单校验或过滤使用
KNOWN_METADATA_KEYS: frozenset[str] = frozenset(
    {
        DocumentMetadataKeys.SOURCE_CHECKSUM,
        DocumentMetadataKeys.LOADER,
        DocumentMetadataKeys.ENCODING,
        DocumentMetadataKeys.PART_INDEX,
        DocumentMetadataKeys.PARENT_DOCUMENT_ID,
        DocumentMetadataKeys.CHUNK_INDEX,
    }
)
