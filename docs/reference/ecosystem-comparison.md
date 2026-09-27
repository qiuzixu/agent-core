# Agent 生态架构对照

本文只比较对 Agent Core 模块边界有直接影响的设计，不以功能数量作为目标。

| 项目 | 值得借鉴 | 当前取舍 |
| --- | --- | --- |
| LangChain Core | Document、Embedding、Retriever、VectorStore、Callback 的协议边界 | 保留小接口，不引入 Runnable/LCEL 和 LangChain 依赖 |
| DeepSeek Harness | Service Definition、Provider、Consumer 分离及按能力拆包 | Core 定义协议，第三方 Provider/适配器保持可选 |
| AutoGen | Core、AgentChat、Extensions 分层和事件驱动 Runtime | 独立 Multi-Agent 扩展采用注册表、事件和显式 handoff，不引入 AutoGen Runtime |
| CrewAI | Knowledge 与 Flow 分离、状态化编排 | Multi-Agent 扩展提供协调原语，业务角色和 Crew 定义仍属于应用 |
| Gemini CLI | hooks、policy、context、telemetry 在 Core，CLI/A2A 分包 | Callback 和事件进入 Core，终端产品能力不进入通用包 |
| MinerU | 多格式解析、OCR、结构化输出和稳定页/块定位 | 独立扩展包提供 HTTP Adapter，Core 不依赖服务运行时 |

当前 Core 采用以下依赖方向：

```text
应用 Agent
  -> Core Protocol / Value Objects
  -> 可选 Provider 或 Adapter
  -> 模型、MinerU、向量数据库等外部服务
```

基础包保持零运行时依赖。Chroma 和 pgvector 适配器通过 optional extras 加载客户端；MinerU 和
Embedding Provider 分别使用独立 `handwritten-agent-core-mineru`、
`handwritten-agent-core-embeddings` 包；多 Agent 协调使用独立
`handwritten-agent-core-multi-agent`。Milvus、Qdrant、通用 OCR 和云文档加载器继续实现 Core
协议并独立发布或由应用提供。这样新 Agent 可以共享稳定接口，又不会被某个平台锁定。
