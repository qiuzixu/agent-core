# Agent 生态架构对照

本文只比较对 Agent Core 模块边界有直接影响的设计，不以功能数量作为目标。

| 项目 | 值得借鉴 | 当前取舍 |
| --- | --- | --- |
| LangChain Core | Document、Embedding、Retriever、VectorStore、Callback 的协议边界 | 保留小接口，不引入 Runnable/LCEL 和 LangChain 依赖 |
| DeepSeek Harness | Service Definition、Provider、Consumer 分离及按能力拆包 | Core 定义协议，第三方 Provider/适配器保持可选 |
| AutoGen | Core、AgentChat、Extensions 分层和事件驱动 Runtime | 当前仓库处于维护模式；只借鉴分层，多 Agent 不进入本轮 Retrieval Core |
| CrewAI | Knowledge 与 Flow 分离、状态化编排 | 借鉴知识层边界；角色和 Crew 属于更高层应用能力 |
| Gemini CLI | hooks、policy、context、telemetry 在 Core，CLI/A2A 分包 | Callback 和事件进入 Core，终端产品能力不进入通用包 |
| MinerU | 多格式解析、OCR、结构化输出和稳定页/块定位 | 作为 Loader/Parser 扩展，不成为 Core 硬依赖 |

当前 Core 采用以下依赖方向：

```text
应用 Agent
  -> Core Protocol / Value Objects
  -> 可选 Provider 或 Adapter
  -> 模型、MinerU、向量数据库等外部服务
```

基础包保持零运行时依赖。OpenAI/Gemini Embedding、pgvector、Milvus、Qdrant、MinerU、OCR 和云文档
加载器应实现 Core 协议并独立发布或由应用提供。这样新 Agent 可以共享稳定接口，又不会被某个解析
平台或向量数据库锁定。
