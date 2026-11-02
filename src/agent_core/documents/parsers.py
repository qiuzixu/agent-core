"""Blob 解析器注册表：按 MIME 类型 / 文件扩展名选择解析器。

Core 默认只内置文本解析；PDF 等二进制格式由独立适配器包
（如 agent-core-mineru）注册进来，本模块不引入任何重依赖。

典型接线方式（以 MinerU 为例）::

    from agent_core.documents.parsers import default_parser_registry
    from agent_core_mineru import MinerUBlobParser

    default_parser_registry.register(
        MinerUBlobParser(endpoint=...),
        mime_types=("application/pdf",),
        extensions=(".pdf",),
    )
"""

from __future__ import annotations

import mimetypes

# BlobParser 协议统一定义在 loaders 模块，这里只做复用，避免出现两个同名协议
from agent_core.documents.loaders import BlobParser, TextBlobParser  # noqa: F401
from agent_core.documents.types import Blob, Document


class ParserNotFoundError(LookupError):
    """找不到能处理该 Blob 的解析器。"""


class ParserRegistry:
    """MIME 类型与扩展名到解析器的映射表。

    匹配优先级：精确 MIME → 通配 MIME（如 text/*）→ 扩展名推断。
    """

    def __init__(self) -> None:
        self._exact: dict[str, BlobParser] = {}
        self._wildcard: list[tuple[str, BlobParser]] = []
        self._extensions: dict[str, BlobParser] = {}

    def register(
        self,
        parser: BlobParser,
        *,
        mime_types: tuple[str, ...] = (),
        extensions: tuple[str, ...] = (),
    ) -> None:
        """注册解析器。mime_types 支持 "type/*" 通配；extensions 需带点且小写。"""
        for mime_type in mime_types:
            normalized = mime_type.strip().lower()
            if not normalized:
                continue
            if normalized.endswith("/*"):
                self._wildcard.append((normalized[:-2], parser))
            else:
                self._exact[normalized] = parser
        for extension in extensions:
            normalized = extension.strip().lower()
            if not normalized:
                continue
            if not normalized.startswith("."):
                normalized = f".{normalized}"
            self._extensions[normalized] = parser

    def resolve(
        self,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> BlobParser | None:
        """按优先级解析出可用的解析器；找不到返回 None。"""
        normalized = (mime_type or "").split(";")[0].strip().lower()
        if normalized:
            parser = self._exact.get(normalized)
            if parser is not None:
                return parser
            main_type = normalized.split("/")[0]
            for prefix, parser in self._wildcard:
                if main_type == prefix:
                    return parser
        if filename:
            guessed, _ = mimetypes.guess_type(filename)
            if guessed and guessed in self._exact:
                return self._exact[guessed]
            extension = filename.rsplit(".", 1)
            if len(extension) == 2:
                parser = self._extensions.get(f".{extension[1].lower()}")
                if parser is not None:
                    return parser
        return None

    async def parse_blob(self, blob: Blob) -> list[Document]:
        """选择解析器并执行解析；没有匹配时抛 ParserNotFoundError。"""
        parser = self.resolve(mime_type=blob.mime_type, filename=blob.source)
        if parser is None:
            raise ParserNotFoundError(
                f"没有注册能处理 mime_type={blob.mime_type!r} source={blob.source!r} 的解析器"
            )
        return await parser.parse(blob)


def _build_default_registry() -> ParserRegistry:
    """构建默认注册表：内置常见文本类型的零依赖解析。"""
    from agent_core.documents.loaders import TextBlobParser

    registry = ParserRegistry()
    registry.register(
        TextBlobParser(),
        mime_types=("text/*", "application/json"),
        extensions=(
            ".txt",
            ".md",
            ".markdown",
            ".json",
            ".csv",
            ".log",
            ".py",
            ".js",
            ".ts",
            ".vue",
            ".yaml",
            ".yml",
        ),
    )
    return registry


#: 进程级默认注册表，业务层可直接在其上注册 PDF 等适配器
default_parser_registry: ParserRegistry = _build_default_registry()


