# 向量存储选型对比

> 覆盖 agent-core `retrieval` 模块内置的全部后端，以及面试/选型中常见的同类产品。
> 工厂的当前默认值以 `retrieval/factory.py` 为准；选型建议不等于自动选择。

## 一、我们提到的所有方案对比

| 项 | Memory（内置） | FAISS | Chroma | pgvector | Qdrant | Milvus | Weaviate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 定位 | 进程内字典 | 向量检索**库** | 向量**数据库** | PG 扩展（向量插件） | 向量数据库 | 分布式向量数据库 | 向量数据库 |
| 部署重量 | 零 | 零（pip 即用） | 低（可嵌入） | 零（复用已有 PG） | 1 容器 | 多容器 + etcd/MinIO | 1 容器 |
| 持久化 | 无 | 本地文件 | 本地/服务端 | PG 表 | 本地/服务端 | 分布式存储 | 本地/服务端 |
| 元数据过滤 | Python 侧 | 全量候选后由适配器过滤 | 支持 | SQL WHERE | 有限候选后由适配器过滤 | 支持 | 支持 |
| 访问控制/namespace | 适配器层统一实现 | 适配器层统一实现 | 适配器层统一实现 | 适配器层统一实现 | Filter 原生下推 | 支持 | 支持 |
| 分布式/水平扩展 | 不支持 | 不支持 | 弱 | 随 PG | 支持 | 强（亿级场景） | 支持 |
| 适合场景 | 测试 | 显式选择的单机索引 | 默认开发环境 | 默认生产环境（需 PG） | 显式选择的独立服务 | 需另行集成 | 需另行集成 |
| agent-core 适配器 | `memory.py` | `faiss.py` | `chroma.py` | `pgvector.py` | `qdrant.py` | 无 | 无 |

## 二、FAISS vs Chroma

| 维度 | FAISS | Chroma |
| --- | --- | --- |
| 定位 | 纯向量检索**库** | 向量**数据库**（带存储引擎、元数据管理） |
| 部署 | 零部署，pip 装完即用 | pip 也能用，但设计上往独立服务走 |
| 元数据/访问控制 | 适配器取全量候选后过滤，数据量增长时成本高 | 由适配器结合后端查询处理 |
| 环境工厂 | 需显式 `backend="faiss"` | `development` 默认 |

两个适配器遵循相同的 `VectorStore` 契约，但过滤执行位置和性能不同。
FAISS 适合明确接受单机文件索引及应用侧过滤成本的场景；开发环境默认使用 Chroma。

## 三、环境默认值与显式选型

| 环境 | 工厂默认后端 | 可显式覆盖 |
| --- | --- | --- |
| test/testing | `memory` | 可指定任一内置后端 |
| development 及其他值 | `chroma` | 可指定 `faiss` 或 `qdrant` 等 |
| production/prod | `pgvector` | 无 PG 时可显式指定 `qdrant` |

`production` 默认选 pgvector，缺少 `postgres_url` 或 `dimension` 会报错，不会自动切换 Qdrant。
FAISS、Chroma、pgvector 和 Qdrant 分别使用对应的 optional extra。

## 四、Qdrant 适配器要点

- 三种连接方式：注入 `client`（测试）> `url`（服务模式/生产）> `path`（本地嵌入式持久化）
- 点 ID 用 `uuid.uuid5` 确定性映射 record_id，重复 upsert 天然覆盖
- namespace + 访问范围下推 Qdrant Filter；业务 metadata 过滤最多拉取 1000 条候选后在 Python 侧匹配，强过滤时需评估召回完整性
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

依赖安装：`pip install zero-agent-core[qdrant]`（`qdrant-client>=1.12,<2`）。

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
