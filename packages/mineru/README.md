# Handwritten Agent Core MinerU

`handwritten-agent-core-mineru` 是 Agent Core 的可选文档解析扩展。它调用已经部署好的 MinerU
自托管 `/file_parse` HTTP 服务，把 Markdown 或 `content_list` 转换成 Core `Document`。

```bash
pip install handwritten-agent-core-mineru
```

```python
from agent_core_mineru import MinerUConfig, MinerUDocumentLoader

loader = MinerUDocumentLoader(
    "./manual.pdf",
    config=MinerUConfig(endpoint="http://127.0.0.1:8000/file_parse"),
)
documents = await loader.load()
```

本包不安装 MinerU、OCR 模型或 GPU 运行时。MinerU 云端异步批处理 API 使用不同协议，可以实现
`MinerUTransport` 后注入。
