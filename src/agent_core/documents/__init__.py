"""文档值对象、加载器、解析器注册表、去重和文本切分器。"""

from agent_core.documents.dedup import deduplicate_chunks, deduplicate_documents
from agent_core.documents.loaders import (
    BlobParser,
    DocumentLoader,
    TextBlobParser,
    TextLoader,
)
from agent_core.documents.metadata import KNOWN_METADATA_KEYS, DocumentMetadataKeys
from agent_core.documents.parsers import (
    ParserNotFoundError,
    ParserRegistry,
    default_parser_registry,
)
from agent_core.documents.splitters import (
    RecursiveCharacterTextSplitter,
    SplitStats,
    TextSplitter,
)
from agent_core.documents.types import Blob, Document, DocumentChunk, DocumentLocator

__all__ = [
    "Blob",
    "BlobParser",
    "Document",
    "DocumentChunk",
    "DocumentLoader",
    "DocumentLocator",
    "DocumentMetadataKeys",
    "KNOWN_METADATA_KEYS",
    "ParserNotFoundError",
    "ParserRegistry",
    "RecursiveCharacterTextSplitter",
    "SplitStats",
    "TextBlobParser",
    "TextLoader",
    "TextSplitter",
    "default_parser_registry",
    "deduplicate_chunks",
    "deduplicate_documents",
]
