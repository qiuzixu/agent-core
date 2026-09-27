# Handwritten Agent Core Embeddings

`handwritten-agent-core-embeddings` 为 Agent Core 的 `Embeddings` 协议提供 OpenAI、Gemini 和
Ollama 实现。Core 的 Retriever 和 VectorStore 不依赖任何厂商 SDK。

```bash
pip install "handwritten-agent-core-embeddings[openai]"
pip install "handwritten-agent-core-embeddings[gemini]"
pip install "handwritten-agent-core-embeddings[ollama]"
```

```python
from agent_core_embeddings import OpenAIEmbeddings

embeddings = OpenAIEmbeddings(
    api_key="从安全配置读取",
    model="text-embedding-3-small",
)
vectors = await embeddings.embed_documents(["第一段", "第二段"])
query_vector = await embeddings.embed_query("查询内容")
```

三个适配器共享批处理、并发、有限重试、超时、有限值和维度校验。API Key 和模型选择由应用配置，
扩展包不读取 `.env`。
