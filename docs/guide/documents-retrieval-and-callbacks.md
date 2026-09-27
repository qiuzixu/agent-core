# 文档、检索与回调

Core 把文档摄取、检索和运行事件定义为独立协议。应用可以替换解析器、Embedding 和向量数据库，
不需要修改 Agent Loop。

## 监听运行事件

```python
from agent_core import AgentRuntime, BaseCallbackHandler, RunEvent


class AuditCallback(BaseCallbackHandler):
    async def on_event(self, event: RunEvent) -> None:
        print(event.event_type, event.run_id, event.component)


runtime = AgentRuntime(agent, callbacks=[AuditCallback()])
```

`CallbackManager` 按注册顺序调用处理器并隔离单个处理器异常。Callback 只观察事件；审批、重试、
裁剪消息或阻止工具仍应使用 Middleware。`EventSink` 消费同一个 `RunEvent`，用于 WebSocket、队列或
审计平台。

## 加载和切分文档

```python
from agent_core import RecursiveCharacterTextSplitter, TextLoader

documents = await TextLoader("./knowledge/manual.txt").load()
splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=80)
chunks = splitter.split_documents(documents)
```

纯文本 Loader 根据来源和 checksum 生成稳定 Document ID，切分器根据父文档和字符区间生成稳定
Chunk ID，重复摄取同一版本时可以幂等覆盖。每个 `DocumentChunk` 都保留 `parent_document_id`、
checksum 和 `DocumentLocator`。Loader 或解析器应
尽可能填写 source、MIME、页码、block id 和字符区间，便于答案引用和增量重建索引。

## 语义检索

应用可以自己实现 `Embeddings`，也可以安装独立 Provider 扩展：

```bash
pip install "handwritten-agent-core-embeddings[openai]"
```

```python
from agent_core_embeddings import EmbeddingExecutionPolicy, create_embeddings

my_embeddings = create_embeddings(
    "openai",
    api_key="从安全配置读取",
    model="text-embedding-3-small",
    dimensions=1536,
    policy=EmbeddingExecutionPolicy(batch_size=64, max_concurrency=2),
)
```

同一个工厂还支持 `provider="gemini"` 和 `provider="ollama"`。然后选择 Core 内存实现或外部
VectorStore 适配器：

```python
from agent_core import (
    AccessContext,
    EmbeddingRetriever,
    InMemoryVectorStore,
    RetrievalQuery,
)

store = InMemoryVectorStore(require_access=True)
retriever = EmbeddingRetriever(my_embeddings, store)
access = AccessContext(user_id="alice", tenant_id="tenant-a")

await retriever.add_documents(
    [chunk.to_document() for chunk in chunks],
    namespace="flight-manual",
    access=access,
)

results = await retriever.retrieve(
    RetrievalQuery(
        "起飞前需要检查什么",
        namespace="flight-manual",
        access=access,
        limit=5,
    )
)
```

生产适配器必须在 VectorStore 内执行 tenant、user、namespace 和 metadata 过滤，不能只在应用取回
结果后过滤。索引任务还应保存来源 checksum、切分配置版本和 Embedding 模型版本，避免不同向量
空间的数据混写。

`handwritten-agent-core-embeddings` 对三个 Provider 统一执行批处理、有限并发、超时、有限重试、
NaN/Infinity 检查和维度检查。Gemini 会分别使用 `RETRIEVAL_DOCUMENT` 和 `RETRIEVAL_QUERY`
task type；Ollama 使用原生 `/api/embed`。扩展包不自动读取 `.env`，密钥仍由应用注入。

## 向量存储分层

```python
from agent_core import create_vector_store

# 测试默认返回 InMemoryVectorStore。
test_store = create_vector_store("testing", require_access=True)

# 开发默认返回 ChromaVectorStore，需要安装 agent-core[chroma]。
dev_store = create_vector_store(
    "development",
    chroma_path="./data/chroma",
    collection_name="flight-manual",
    require_access=True,
)

# 生产默认返回 PgVectorStore，需要安装 agent-core[pgvector] 并完成异步初始化。
prod_store = create_vector_store(
    "production",
    postgres_url="postgresql://user:password@host/database",
    dimension=1536,
    collection_name="flight-manual",
    require_access=True,
)
await prod_store.initialize()
```

Chroma 同时持久化向量、Document 和 metadata，适合本地开发。FAISS 是高性能索引库，
不直接提供完整的文档存储、metadata 查询、租户权限和并发写语义，因此留作后续离线适配器。
pgvector 复用 PostgreSQL 的事务、备份和部署体系，并在数据库查询中执行 namespace、JSONB metadata
及用户/租户过滤。

`backend="memory" | "chroma" | "pgvector"` 可以覆盖环境默认值。Core 不会在缺少依赖或生产连接
参数时静默降级。pgvector 的一个表只容纳一种向量维度；更换 Embedding 模型导致维度变化时，
应使用新表并重新摄取文档。

## 安全序列化

```python
from agent_core import dumps, loads

encoded = dumps(documents[0])
restored = loads(encoded)
```

序列化信封包含稳定 `type` 和 `schema_version`。自定义对象必须显式注册编码器、解码器和版本迁移，
Core 不接受类路径动态导入或 pickle。

## 使用 MinerU 解析复杂文档

安装可选 HTTP 依赖后，可以调用已经部署好的 MinerU 自托管服务：

```bash
pip install handwritten-agent-core-mineru
```

```python
from agent_core_mineru import MinerUConfig, MinerUDocumentLoader

loader = MinerUDocumentLoader(
    "./knowledge/flight-manual.pdf",
    config=MinerUConfig(
        endpoint="http://127.0.0.1:8000/file_parse",
        language="ch",
        output_mode="blocks",
    ),
    metadata={"namespace": "flight-manual"},
)
documents = await loader.load()
```

`output_mode="blocks"` 优先把 `content_list` 中可检索的文字、表格、公式和图片说明分别转换成
`Document`，并在 `DocumentLocator` 中保留页码和 block id。`output_mode="markdown"` 返回完整
Markdown，适合先保留版面结构，再交给 `RecursiveCharacterTextSplitter` 切分。

该能力由独立 `handwritten-agent-core-mineru` 包提供，不进入 `agent_core` 顶层 API。适配器面向
MinerU 自托管 `/file_parse` multipart 接口，读取常见的 `md_content` 和
`content_list` 响应。MinerU 云端异步批处理、预签名上传和结果压缩包下载采用不同协议，需要应用
实现 `MinerUTransport` 后注入。自定义网关、认证和测试替身也使用同一传输端口。

扩展包只包含 HTTP 适配和响应转换。Core 只保留 `DocumentLoader`、`BlobParser`、`Document` 及
通用文档异常。MinerU 模型、OCR 运行时、GPU 资源及服务部署仍由应用或基础设施负责。
