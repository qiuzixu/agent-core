# 向量存储选型对比

> 覆盖 agent-core `retrieval` 模块内置的全部后端，以及面试/选型中常见的同类产品。
> 结论先行：**部署重量和收益成正比才引入**（与 Apollo 4 容器的判断逻辑一致）。

## 一、我们提到的所有方案对比

| 项 | Memory（内置） | FAISS | Chroma | pgvector | Qdrant | Milvus | Weaviate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 定位 | 进程内字典 | 向量检索**库** | 向量**数据库** | PG 扩展（向量插件） | 向量数据库 | 分布式向量数据库 | 向量数据库 |
| 部署重量 | 零 | 零（pip 即用） | 低（可嵌入） | 零（复用已有 PG） | 1 容器 | 多容器 + etcd/MinIO | 1 容器 |
| 持久化 | 无 | 本地文件 | 本地/服务端 | PG 表 | 本地/服务端 | 分布式存储 | 本地/服务端 |
| 元数据过滤 | Python 侧 | 不支持（需自建） | 支持 | SQL WHERE | 支持（Filter） | 支持 | 支持 |
| 访问控制/namespace | 适配器层统一实现 | 适配器层统一实现 | 适配器层统一实现 | 适配器层统一实现 | Filter 原生下推 | 支持 | 支持 |
| 分布式/水平扩展 | 不支持 | 不支持 | 弱 | 随 PG | 支持 | 强（亿级场景） | 支持 |
| 适合场景 | 测试 | 单机开发 | 单机开发（功能多余） | 生产（已有 PG） | 生产（无 PG 或需独立服务） | 亿级+高吞吐 | 生产 |
| agent-core 适配器 | `memory.py` | `faiss.py` | `chroma.py` | `pgvector.py` | `qdrant.py` | 无 | 无 |

## 二、FAISS vs Chroma（为什么开发选 FAISS）

| 维度 | FAISS | Chroma |
| --- | --- | --- |
| 定位 | 纯向量检索**库** | 向量**数据库**（带存储引擎、元数据管理） |
| 部署 | 零部署，pip 装完即用 | pip 也能用，但设计上往独立服务走 |
| 元数据/访问控制 | 我们自己在 `_common.py` 里做 | 它自带，但我们已经在适配器层统一做了 |
| 我们的需求 | 本地持久化 + 进程内检索 | 一样 |

关键点：我们的 namespace / 租户 / 访问控制逻辑跨所有后端统一实现（`_common.py`），Chroma 自带的元数据管理用不上，属于多余依赖。

## 三、环境选型矩阵（agent-core 现行策略）

| 环境 | 推荐后端 | 理由 |
| --- | --- | --- |
| test/testing | `memory` | 快、 ephemeral，无需清理 |
| development | `faiss` | 零部署 + 本地持久化 |
| production（已有 PG） | `pgvector` | 0 个新容器，复用现有数据库 |
| production（无 PG） | `qdrant` | 1 容器，带原生过滤下推 |
| 亿级向量 + 高吞吐 | Milvus（未内置） | 需要分布式才值得多容器成本 |

## 四、Qdrant 适配器要点（新增）

- 三种连接方式：注入 `client`（测试）> `url`（服务模式/生产）> `path`（本地嵌入式持久化）
- 点 ID 用 `uuid.uuid5` 确定性映射 record_id，重复 upsert 天然覆盖
- namespace + 访问范围下推 Qdrant Filter；业务 metadata 过滤超额拉取 1000 条后 Python 侧精确匹配
- 集合不存在：search 返回 `[]`、delete 返回 0，与 Memory/Chroma 行为一致
- `close()` 只关闭自建 client

```python
# 本地嵌入式（开发）
create_vector_store("development", backend="qdrant", qdrant_path="./qdrant_db")

# 服务模式（生产）
create_vector_store("production", backend="qdrant",
                    qdrant_url="http://localhost:6333",
                    qdrant_api_key="...")

# 注入已有 client（测试）
create_vector_store("test", backend="qdrant", qdrant_client=fake_client)
```

依赖安装：`pip install agent-core[qdrant]`（`qdrant-client>=1.12,<2`）。

## 五、pgvector vs Qdrant：各自的缺失能力

核心结论：**pgvector 缺的是"向量规模能力"，Qdrant 缺的是"数据库能力"**。

### 选 pgvector 时放弃的（vs Qdrant）

| 缺失能力 | 具体表现 |
| --- | --- |
| 分布式扩展 | 绑死单 PG 节点；亿级向量需 Citus 等方案，无原生分片+副本 |
| 混合检索 | 无原生 sparse vector；BM25 靠 tsvector 自己拼，RRF 融合手写 SQL；Qdrant Query API 原生支持 sparse+dense 融合 |
| 过滤+向量联合检索性能 | HNSW 遇强过滤条件召回率下降；Qdrant filterable HNSW 专门优化 |
| 量化压缩 | 仅 halfvec/bit；Qdrant 内置 scalar/product/binary 量化，内存省 4-32 倍 |
| 专用快照 | 靠 PG 生态间接覆盖，非向量专用优化 |

### 选 Qdrant 时放弃的（vs pgvector）

| 缺失能力 | 具体表现 |
| --- | --- |
| ACID 事务 | 向量写入与业务表写入不在同一事务，需自行补偿一致性 |
| JOIN / 关系查询 | 向量与业务数据分家；pgvector 一条 SQL 完成 |
| 备份/恢复生态 | 无 WAL/PITR/流复制；快照全量，恢复粒度不如 PG |
| 运维栈成本 | 多一个独立监控/升级/备份系统；pgvector 是 CREATE EXTENSION 一行 |
| SQL 生态 | 行级安全（RLS）、视图、权限体系不可用；namespace/租户控制需 Qdrant Filter 自拼 |

### 场景决策表

| 场景 | 谁的短板触发 | 推荐 |
| --- | --- | --- |
| 已有 PG，百万级以下向量 | Qdrant 的多运维栈+无事务先痛 | pgvector |
| 无 PG，需要独立服务 | pgvector 不在选项内 | Qdrant |
| 亿级 + 高吞吐 | pgvector 单机上限先痛 | Qdrant 集群 |
| 强事务一致 + 复杂关系过滤 | Qdrant 无 JOIN 先痛 | pgvector |

一句话总结：**pgvector 输在"向量做大了之后"，Qdrant 输在"数据库该有的日常"**。本项目（检索量不大、已有 PG 的生产环境）落在 pgvector 甜区，故矩阵首选 pgvector。
