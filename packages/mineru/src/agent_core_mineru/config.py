"""MinerU 扩展配置。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

MinerUOutputMode = Literal["blocks", "markdown"]


@dataclass(frozen=True)
class MinerUConfig:
    """MinerU ``/file_parse`` 请求配置。

    扩展包只调用已经部署好的 MinerU HTTP 服务，不负责下载模型或启动 OCR 运行时。
    ``endpoint`` 应指向 MinerU 自托管服务的 ``/file_parse`` 路由。
    """

    endpoint: str = "http://127.0.0.1:8000/file_parse"
    api_key: str | None = None
    timeout_seconds: float = 300.0
    backend: str = "pipeline"
    parse_method: str = "auto"
    language: str = "ch"
    formula_enabled: bool = True
    table_enabled: bool = True
    start_page_id: int = 0
    end_page_id: int = 99999
    output_mode: MinerUOutputMode = "blocks"
    parser_version: str = "mineru-http-v1"
    max_file_bytes: int = 100 * 1024 * 1024
    headers: Mapping[str, str] = field(default_factory=dict)
    extra_form_fields: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MinerU endpoint 必须是有效的 http 或 https URL")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if self.start_page_id < 0:
            raise ValueError("start_page_id 不能小于 0")
        if self.end_page_id < self.start_page_id:
            raise ValueError("end_page_id 不能小于 start_page_id")
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes 必须大于 0")
        if not self.parser_version.strip():
            raise ValueError("parser_version 不能为空")
