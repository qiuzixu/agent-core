"""MinerU 文档适配器测试。"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_core import (
    Blob,
    DocumentParsingError,
    DocumentServiceError,
)

from agent_core_mineru import (
    HttpxMinerUTransport,
    MinerUBlobParser,
    MinerUConfig,
    MinerUDocumentLoader,
)


class _FakeTransport:
    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = response
        self.blobs: list[Blob] = []
        self.configs: list[MinerUConfig] = []

    async def parse(self, blob: Blob, *, config: MinerUConfig) -> Mapping[str, Any]:
        self.blobs.append(blob)
        self.configs.append(config)
        return self.response


class _FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> object:
        return self._payload


class _FakeHttpClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.requests.append({"url": url, **kwargs})
        return self.response


class MinerUParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_preserve_page_lineage_and_stable_ids(self) -> None:
        response = {
            "results": {
                "manual": {
                    "version": "2.1.0",
                    "md_content": "# 飞行手册\n\n起飞前检查。",
                    "content_list": [
                        {"type": "title", "text": "飞行手册", "page_idx": 0, "block_id": "b-1"},
                        {"type": "text", "text": "起飞前检查。", "page_idx": 1},
                    ],
                }
            }
        }
        transport = _FakeTransport(response)
        parser = MinerUBlobParser(transport=transport)
        blob = Blob(b"pdf", source="C:/docs/manual.pdf", mime_type="application/pdf")

        first = await parser.parse(blob)
        second = await parser.parse(blob)

        self.assertEqual([document.content for document in first], ["飞行手册", "起飞前检查。"])
        self.assertEqual(
            [document.document_id for document in first],
            [document.document_id for document in second],
        )
        assert first[0].locator is not None
        assert first[1].locator is not None
        self.assertEqual(first[0].locator.page, 1)
        self.assertEqual(first[0].locator.block_id, "b-1")
        self.assertEqual(first[1].locator.page, 2)
        self.assertEqual(first[1].metadata["parser"], "mineru")
        self.assertEqual(first[1].metadata["mineru_version"], "2.1.0")

    async def test_markdown_mode_uses_full_markdown(self) -> None:
        transport = _FakeTransport(
            {
                "data": {
                    "results": {
                        "document.pdf": {
                            "md_content": "# 标题\n\n正文",
                            "content_list": [{"type": "text", "text": "正文", "page_idx": 0}],
                        }
                    }
                }
            }
        )
        parser = MinerUBlobParser(
            MinerUConfig(output_mode="markdown"),
            transport=transport,
        )

        documents = await parser.parse(Blob(b"pdf", source="document.pdf"))

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].content, "# 标题\n\n正文")
        self.assertEqual(documents[0].metadata["mineru_block_count"], 1)

    async def test_loader_reads_local_file_and_propagates_metadata(self) -> None:
        transport = _FakeTransport(
            {"results": {"manual": {"md_content": "内容", "content_list": []}}}
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.pdf"
            path.write_bytes(b"pdf-data")
            loader = MinerUDocumentLoader(
                path,
                transport=transport,
                metadata={"namespace": "manual"},
            )

            documents = await loader.load()

        self.assertEqual(transport.blobs[0].data, b"pdf-data")
        self.assertEqual(transport.blobs[0].mime_type, "application/pdf")
        self.assertEqual(documents[0].metadata["namespace"], "manual")

    async def test_invalid_content_list_and_service_error_are_explicit(self) -> None:
        malformed = MinerUBlobParser(
            transport=_FakeTransport({"results": {"document": {"content_list": "{"}}})
        )
        failed = MinerUBlobParser(transport=_FakeTransport({"code": 500, "msg": "模型不可用"}))

        with self.assertRaises(DocumentParsingError):
            await malformed.parse(Blob(b"pdf", source="document.pdf"))
        with self.assertRaises(DocumentServiceError):
            await failed.parse(Blob(b"pdf", source="document.pdf"))

    async def test_file_size_limit_is_checked_before_transport(self) -> None:
        transport = _FakeTransport({})
        parser = MinerUBlobParser(MinerUConfig(max_file_bytes=2), transport=transport)

        with self.assertRaises(DocumentParsingError):
            await parser.parse(Blob(b"pdf"))
        self.assertEqual(transport.blobs, [])


class MinerUTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_transport_builds_multipart_request(self) -> None:
        client = _FakeHttpClient(_FakeResponse({"results": {}}))
        transport = HttpxMinerUTransport(client)
        config = MinerUConfig(
            endpoint="http://mineru.local/file_parse",
            api_key="secret",
            formula_enabled=False,
            extra_form_fields={"return_middle_json": "false"},
        )

        payload = await transport.parse(
            Blob(b"pdf", source="manual.pdf", mime_type="application/pdf"),
            config=config,
        )

        self.assertEqual(payload, {"results": {}})
        request = client.requests[0]
        self.assertEqual(request["url"], config.endpoint)
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(request["data"]["formula_enable"], "false")
        self.assertEqual(request["data"]["return_content_list"], "true")
        self.assertEqual(request["files"]["files"][0], "manual.pdf")

    async def test_http_error_is_wrapped(self) -> None:
        client = _FakeHttpClient(_FakeResponse({}, status_code=503, text="unavailable"))
        transport = HttpxMinerUTransport(client)

        with self.assertRaises(DocumentServiceError):
            await transport.parse(Blob(b"pdf"), config=MinerUConfig())


if __name__ == "__main__":
    unittest.main()
