"""MinerU 响应到 Agent Core Document 的转换。"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from agent_core import Blob, Document, DocumentLocator, DocumentParsingError, DocumentServiceError

from agent_core_mineru.config import MinerUConfig
from agent_core_mineru.transport import HttpxMinerUTransport, MinerUTransport, source_filename


class MinerUBlobParser:
    """把 MinerU Markdown 或 content list 响应转换成 Core Document。"""

    def __init__(
        self,
        config: MinerUConfig | None = None,
        *,
        transport: MinerUTransport | None = None,
    ) -> None:
        self._config = config or MinerUConfig()
        self._transport = transport or HttpxMinerUTransport()

    async def parse(self, blob: Blob) -> list[Document]:
        if not blob.data:
            raise DocumentParsingError("不能解析空文件")
        if len(blob.data) > self._config.max_file_bytes:
            raise DocumentParsingError(
                f"文件大小 {len(blob.data)} 字节超过限制 {self._config.max_file_bytes} 字节"
            )

        payload = await self._transport.parse(blob, config=self._config)
        result = _resolve_result(payload, source_filename(blob.source))
        _raise_for_service_error(payload, result)

        blocks = _read_blocks(result)
        block_documents = self._build_block_documents(blob, blocks, result)
        markdown = _read_text(result, ("md_content", "markdown", "content", "text"))

        if self._config.output_mode == "blocks" and block_documents:
            return block_documents
        if markdown:
            return [self._build_markdown_document(blob, markdown, result, len(blocks))]
        if block_documents:
            merged = "\n\n".join(document.content for document in block_documents)
            return [self._build_markdown_document(blob, merged, result, len(blocks))]
        raise DocumentParsingError("MinerU 响应中没有可用的 Markdown 或文本区块")

    def _build_block_documents(
        self,
        blob: Blob,
        blocks: list[Mapping[str, Any]],
        result: Mapping[str, Any],
    ) -> list[Document]:
        documents: list[Document] = []
        source_key = blob.source or f"blob:{blob.checksum}"
        mineru_version = _read_scalar_text(result, ("version", "mineru_version"))
        for index, block in enumerate(blocks):
            content = _block_content(block)
            if not content:
                continue
            page = _block_page(block)
            block_id = _read_scalar_text(block, ("block_id", "id")) or str(index)
            block_type = _read_scalar_text(block, ("type", "block_type")) or "text"
            identity = (
                f"{source_key}:{blob.checksum}:{self._config.parser_version}:"
                f"{page}:{block_id}:{content}"
            )
            metadata: dict[str, Any] = {
                **blob.metadata,
                "source_checksum": blob.checksum,
                "parser": "mineru",
                "parser_version": self._config.parser_version,
                "mineru_block_index": index,
                "mineru_block_type": block_type,
            }
            if mineru_version:
                metadata["mineru_version"] = mineru_version
            documents.append(
                Document(
                    document_id=str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                    content=content,
                    source=blob.source,
                    mime_type="text/markdown",
                    metadata=metadata,
                    locator=DocumentLocator(uri=blob.source, page=page, block_id=block_id),
                )
            )
        return documents

    def _build_markdown_document(
        self,
        blob: Blob,
        markdown: str,
        result: Mapping[str, Any],
        block_count: int,
    ) -> Document:
        source_key = blob.source or f"blob:{blob.checksum}"
        identity = f"{source_key}:{blob.checksum}:{self._config.parser_version}:markdown"
        metadata: dict[str, Any] = {
            **blob.metadata,
            "source_checksum": blob.checksum,
            "parser": "mineru",
            "parser_version": self._config.parser_version,
            "mineru_block_count": block_count,
        }
        mineru_version = _read_scalar_text(result, ("version", "mineru_version"))
        if mineru_version:
            metadata["mineru_version"] = mineru_version
        return Document(
            document_id=str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
            content=markdown,
            source=blob.source,
            mime_type="text/markdown",
            metadata=metadata,
            locator=DocumentLocator(uri=blob.source),
        )


class MinerUDocumentLoader:
    """读取本地文件并通过 MinerU HTTP 服务解析。"""

    def __init__(
        self,
        path: str | Path,
        *,
        config: MinerUConfig | None = None,
        transport: MinerUTransport | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._path = Path(path)
        self._parser = MinerUBlobParser(config, transport=transport)
        self._metadata = dict(metadata or {})

    async def load(self) -> list[Document]:
        try:
            data = await asyncio.to_thread(self._path.read_bytes)
        except OSError as exc:
            raise DocumentParsingError(f"读取文档失败：{self._path}") from exc
        mime_type, _ = mimetypes.guess_type(self._path.name)
        blob = Blob(
            data=data,
            source=str(self._path.resolve()),
            mime_type=mime_type or "application/octet-stream",
            metadata=self._metadata,
        )
        return await self._parser.parse(blob)

    async def lazy_load(self) -> AsyncIterator[Document]:
        for document in await self.load():
            yield document


def _has_output_fields(value: Mapping[str, Any]) -> bool:
    return any(key in value for key in ("md_content", "markdown", "content_list", "blocks"))


def _resolve_result(payload: Mapping[str, Any], filename: str) -> Mapping[str, Any]:
    current = payload
    for _ in range(4):
        if _has_output_fields(current):
            return current
        data = current.get("data")
        if isinstance(data, Mapping):
            current = data
            continue
        results = current.get("results")
        if isinstance(results, Mapping):
            current = _select_file_result(results, filename)
            continue
        break
    return current


def _select_file_result(results: Mapping[str, Any], filename: str) -> Mapping[str, Any]:
    if _has_output_fields(results):
        return results
    stem = Path(filename).stem
    for key in (filename, stem):
        value = results.get(key)
        if isinstance(value, Mapping):
            return value
    mappings = [value for value in results.values() if isinstance(value, Mapping)]
    if len(mappings) == 1:
        return mappings[0]
    raise DocumentParsingError("MinerU 响应包含多个文件，但无法匹配当前文件名")


def _raise_for_service_error(
    payload: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    code = payload.get("code")
    if isinstance(code, int) and code != 0:
        message = _read_scalar_text(payload, ("msg", "message", "error")) or str(code)
        raise DocumentServiceError(f"MinerU 解析失败：{message}")
    error = _read_scalar_text(result, ("error", "err_msg"))
    if error:
        raise DocumentServiceError(f"MinerU 解析失败：{error}")


def _read_blocks(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = result.get("content_list", result.get("blocks"))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DocumentParsingError("MinerU content_list 不是有效 JSON") from exc
    if value is None:
        return []
    if not isinstance(value, list):
        raise DocumentParsingError("MinerU content_list 必须是数组")
    return [item for item in value if isinstance(item, Mapping)]


def _block_content(block: Mapping[str, Any]) -> str:
    direct = _read_text(block, ("text", "content", "md_content", "table_body", "latex"))
    if direct:
        return direct
    for key in ("image_caption", "image_footnote", "table_caption", "table_footnote"):
        value = block.get(key)
        if isinstance(value, list):
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                return "\n".join(parts)
    return ""


def _block_page(block: Mapping[str, Any]) -> int | None:
    page_index = block.get("page_idx")
    if isinstance(page_index, int) and page_index >= 0:
        return page_index + 1
    for key in ("page_no", "page"):
        value = block.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _read_text(value: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _read_scalar_text(value: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            text = str(item).strip()
            if text:
                return text
    return ""
