"""MinerU HTTP 传输实现。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote, urlparse

from agent_core import Blob, DocumentParsingError, DocumentServiceError

from agent_core_mineru.config import MinerUConfig


@runtime_checkable
class MinerUTransport(Protocol):
    """MinerU 请求传输端口，便于替换认证、网关或测试实现。"""

    async def parse(self, blob: Blob, *, config: MinerUConfig) -> Mapping[str, Any]: ...


class HttpxMinerUTransport:
    """使用 httpx 调用 MinerU ``/file_parse`` 接口。"""

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    async def parse(self, blob: Blob, *, config: MinerUConfig) -> Mapping[str, Any]:
        filename = source_filename(blob.source)
        headers = {"Accept": "application/json", **dict(config.headers)}
        if config.api_key:
            headers.setdefault("Authorization", f"Bearer {config.api_key}")
        form = {
            "backend": config.backend,
            "parse_method": config.parse_method,
            "lang_list": config.language,
            "formula_enable": _form_bool(config.formula_enabled),
            "table_enable": _form_bool(config.table_enabled),
            "return_md": "true",
            "return_content_list": "true",
            "return_images": "false",
            "start_page_id": str(config.start_page_id),
            "end_page_id": str(config.end_page_id),
            **dict(config.extra_form_fields),
        }
        files = {
            "files": (
                filename,
                blob.data,
                blob.mime_type or "application/octet-stream",
            )
        }

        if self._client is not None:
            return await self._post(
                self._client,
                config=config,
                headers=headers,
                form=form,
                files=files,
            )

        import httpx

        try:
            async with httpx.AsyncClient() as client:
                return await self._post(
                    client,
                    config=config,
                    headers=headers,
                    form=form,
                    files=files,
                )
        except DocumentServiceError:
            raise
        except Exception as exc:
            raise DocumentServiceError(f"MinerU 服务调用失败：{exc}") from exc

    @staticmethod
    async def _post(
        client: Any,
        *,
        config: MinerUConfig,
        headers: Mapping[str, str],
        form: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> Mapping[str, Any]:
        try:
            response = await client.post(
                config.endpoint,
                headers=dict(headers),
                data=dict(form),
                files=dict(files),
                timeout=config.timeout_seconds,
            )
        except Exception as exc:
            raise DocumentServiceError(f"MinerU 服务调用失败：{exc}") from exc

        status_code = int(getattr(response, "status_code", 0))
        if status_code < 200 or status_code >= 300:
            response_text = str(getattr(response, "text", ""))[:500]
            raise DocumentServiceError(f"MinerU 服务返回 HTTP {status_code}：{response_text}")
        try:
            payload = response.json()
        except Exception as exc:
            raise DocumentParsingError("MinerU 服务返回了无效 JSON") from exc
        if not isinstance(payload, Mapping):
            raise DocumentParsingError("MinerU 响应根节点必须是对象")
        return payload


def source_filename(source: str | None) -> str:
    """从本地路径或 URL 提取上传文件名。"""
    if not source:
        return "document.bin"
    parsed = urlparse(source)
    path = unquote(parsed.path) if parsed.scheme else source
    filename = Path(path).name
    return filename or "document.bin"


def _form_bool(value: bool) -> str:
    return "true" if value else "false"
