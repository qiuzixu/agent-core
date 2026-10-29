# Retrieval 模块整体文档

> 更新时间：2026-10-29。覆盖 Rerank 精排、Hybrid RRF 混合检索、知识持久化（双端同步）。

## 1. 模块定位

Retrieval 是 agent-core 的知识检索内核，位于业务入口（KnowledgeService / knowledge.py）之下，
为对话链路提供"查知识"能力。设计原则：**每层能力独立可开关，关到底就是纯进程内关键词检索，全开就是生产级混合检索管线**。

```
用户提问
   |
   v
KnowledgeService / knowledge.py          <- 业务入口（双端各自实现）
   |            |
   |            v
   |    KnowledgeDocumentStore  <- P3 持久化（SQLite / InMemory）
   |
   v
agent-core / retrieval/                  <- 检索内核
   KeywordRetriever   （BM25 词面路）
 + SemanticRetriever（embedding + 向量库，语义路）
 + HybridRetriever  （RRF 融合）
 + RerankClient     （二阶段精排）
```

## 2. 写入链路（POST /knowledge）

1. 生成 document_id，写入内存索引（_documents + _term_frequencies 词频表）。
2. 语义路（knowledge_embedding_enabled）：embeddings.embed(content) 后写入向量库，记录内部 id 映射。
3. 持久化路（knowledge_persistence_enabled）：KnowledgeDocumentStore.upsert 落 SQLite。

## 3. 检索链路（GET /knowledge/search）

```
阶段 0：惰性回灌（仅首次访问触发）
  SQLite 全量读出 -> 重建 BM25 词频索引 -> 逐条重建语义向量 -> 重建 vector_id 映射
阶段 1：双路召回
  BM25 路（永远开）：中文二元切分 -> 词面打分
  语义路（开关）：query -> embedding -> 向量库 cosine 召回 top_k * multiplier
阶段 2：RRF 融合（开关）
  score = sum( 1 / (60 + rank_i) )   只看排名不看分数，天然免疫量纲差异
  双路都召回的条目分数叠加，自然排前
阶段 3：Rerank 精排（开关）
  召回 top_k * 3 条候选（recall_multiplier）-> POST {base_url}/rerank
  cross-encoder 返回 relevance_score 重排，取 top_k
  失败/超时：诚实降级为 RRF 原始排序，不阻塞检索
```

## 4. 删除链路（DELETE /knowledge/{id}）

三清对齐（任何一处残留都会导致"删了还搜到"）：

1. 内存 BM25 索引：_documents.pop(id) + _term_frequencies.pop(id)
2. 语义向量：vector_store.delete(_vector_ids.pop(id))（agent-core memory.py 新增 delete_documents）
3. 持久层：KnowledgeDocumentStore.delete(id)

## 5. 文本匹配手段谱系（项目实际用到 4 种）

| # | 手段 | 技术本质 | 解决什么 |
| --- | --- | --- | --- |
| 1 | 中文二元切分 + 词面匹配 | n-gram 重合统计 | 无模型依赖的兜底 |
| 2 | BM25 | TF-IDF 改进（词频 x 逆文档频率 x 长度归一化） | 精确词匹配、编号、专名 |
| 3 | 稠密向量检索 | embedding 压成 1536 维数组 + cosine | 同义改写、跨表述 |
| 4 | Rerank 精排 | cross-encoder 把 query+doc 拼一起打相关性分 | 排序层，召回多但序不准的问题 |

前 3 种是召回层（尽量不漏），第 4 种是排序层（尽量排准）。

### 常见概念澄清

- **BM25 不是向量**：是打分函数（TF-IDF 家族），靠倒排索引逐词算分，全程没有向量。
- **TF-IDF + cosine** 是 BM25 的前身（传统向量空间模型），BM25 用词频饱和因子 k1、
  长度归一化 b 和可调参数改进了它，工程上选 BM25 不选 TF-IDF cosine。
- 真正的语义泛化靠 embedding 稠密向量，与 BM25 互补不重叠——这是双路召回的根本理由。

## 6. 三个核心机制"有什么用"

| 机制 | 一句话 |
| --- | --- |
| 双路召回 | 关键词管精确（编号/专名），语义管理解（同义改写），两个都跑才不漏 |
| RRF 融合 | 只看排名不看分数：两路分数量纲不同没法直接加；两路都认可的自然排前面 |
| 持久化 | 重启不失忆；惰性回灌（首次访问才重建索引）不拖启动 |

## 7. 配置开关（全部默认关闭）

| 开关 | 作用 | 关闭时行为 |
| --- | --- | --- |
| knowledge_persistence_enabled | SQLite 落盘 + 重启回灌 | 纯内存，重启丢失 |
| knowledge_embedding_enabled | 语义向量路 | 只有 BM25 词面匹配 |
| knowledge_hybrid_enabled | RRF 融合 | 单路结果直出 |
| knowledge_rerank_enabled | 二阶段精排 | 召回序即最终序 |

## 8. 向量存储选型

见同目录 vector-store-comparison.md。本地默认 InMemoryVectorStore；
生产可选 Qdrant（已内置适配器）/ pgvector（复用业务库）/ FAISS（检索库非数据库）；
Chroma 功能全但默认本地持久化模型与本项目分层配置思路不契合，未默认集成。

## 9. 测试基线（2026-10-29）

- agent-core：345 passed / 21 skipped / 2 xfailed
- my-cesium-agent-vanilla：241 passed（含 knowledge_persistence 2 例）
- my-cesium-agent：380 passed（含 knowledge_persistence 3 例）

## 10. 面试一段话总结

"我们的检索管线是三层漏斗：召回层并行跑 BM25 词面路和 embedding 语义路，
用 RRF 按排名融合，免疫两路分数量纲不一致的问题；再过 cross-encoder rerank 精排。
BM25 兜住精确匹配，向量兜住同义改写，rerank 解决召回多但序不准的问题。
知识条目有 SQLite 持久化 + 惰性回灌，重启不失忆。每层独立开关，
最小部署可以只跑 BM25，生产全开就是完整的 hybrid search 管线。"