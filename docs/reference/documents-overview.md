# 文档处理概览

`agent_core.documents` 提供与模型、存储实现无关的文档值对象、加载器和切分器，
是"摄入 → 切分 → 检索"链路的最上游模块。整个模块 4 个文件、零第三方依赖。

## 模块结构

```
documents/
├── types.py      # 数据契约：Blob / DocumentLocator / Document / DocumentChunk
├── loaders.py    # 加载器：TextLoader / TextBlobParser（文本 → Document）
└── splitters.py  # 切分器：RecursiveCharacterTextSplitter（Document → Chunk）
```

三层职责对应一条流水线：

```
文件/字节流 ──(Loader)──▶ Document ──(Splitter)──▶ DocumentChunk[] ──▶ 检索索引
```

## 数据契约（types.py）

### 四个值对象

| 对象 | 含义 | 关键点 |
| --- | --- | --- |
| `Blob` | 尚未解析的原始二进制 | `data: bytes` + 自动计算 `sha256` checksum |
| `DocumentLocator` | 文档中的稳定位置 | uri / page / block_id / start_offset / end_offset，用于引用与溯源 |
| `Document` | 解析后的文本及来源信息 | `document_id` + 自动 checksum |
| `DocumentChunk` | 可索引的文档片段 | 保留 `parent_document_id` 和字符区间，可 `to_document()` 还原 |

### checksum 机制

`Blob`、`Document`、`DocumentChunk` 在 `__post_init__` 中自动计算
`sha256` 并写入 `checksum` 字段，作用：

- 完整性校验——内容被篡改后 checksum 对不上；
- 去重依据——相同内容的文档/片段 checksum 一致；
- 溯源——chunk 可通过 checksum 关联回父文档。

checksum 校验规则（Document / DocumentChunk 一致）：
构造与 `from_dict` 恢复时，checksum 非空则**严格校验** sha256 与内容一致，
不一致抛 `ValueError`（判定篡改）；为空则自动计算（向后兼容旧数据）。
严格校验 `Blob` 由 serialization 模块在恢复时执行。

## 加载器（loaders.py）

- `TextBlobParser`：实现 `BlobParser` 协议，把 `Blob` 解码为 `Document`，
  保留 `source` / `mime_type`，并把 `blob.checksum` 写入 `metadata.source_checksum`；
- `TextLoader`：读本地 UTF 文本 → 构造 `Blob`（mime 由文件名猜测）→ 交给 parser；
  文件读取通过 `asyncio.to_thread` 避免阻塞事件循环；
- `lazy_load()`：当前是先 `load()` 全量再逐个 yield，属"伪流式"，
  大文件场景是升级点。

当前只内置文本类解析，PDF / Word 等二进制格式通过外部适配器接入
（见下文 MinerU），不在本模块内引入重依赖。

## 切分器（splitters.py）

`RecursiveCharacterTextSplitter` 的策略是：**按分隔符优先级从高到低，在
chunk 内部靠后的位置找最后一个切点**，尽量让每个片段语义完整。

分隔符优先级：`\n\n` → `\n` → `。` → `；` → `. ` → 空格。

关键参数：

| 参数 | 默认值 | 语义 |
| --- | --- | --- |
| `chunk_size` | 1000 | 单个 chunk 最大字符数 |
| `chunk_overlap` | 100 | 相邻 chunk 重叠字符数，防止答案被切断 |
| `separators` | 见上 | 自定义切分边界（如传入 `"\n\n"` 适配题目分段的语料） |

实现细节：

- 硬上限兜底：找不到任何分隔符时按 `chunk_size` 硬切；
- chunk ID 用 `uuid5(NAMESPACE_URL, "{doc_id}:{index}:{start}:{end}")`
  生成——**确定性 ID**，同文档同切法重复切分得到相同 ID，天然幂等可去重；
- 切分 offset 会叠加父文档 locator 的 `start_offset`，
  保证 chunk 在原始文件里的绝对位置可回溯。

## 与其他模块的关系

| 模块 | 关系 |
| --- | --- |
| `retrieval` | 消费 `DocumentChunk` 建索引（BM25 / 向量双路） |
| `serialization` | `Blob` / `Document` / `DocumentChunk` 注册为 codec，跨进程传输 |
| `packages/mineru` | PDF 等二进制格式的外部解析适配器（独立包，按需安装） |
| 业务层 | 字段级抽取（如题库的题干/答案/解析拆分）属于业务层，不进 Core |

## 题库场景（走读讨论结论）

**场景**：一份题库 PDF，含题目标题、题目内容、答案、解析，要求检索后原文直出。

### 核心设计：一题一个 Document，不拆四个独立文档

```
Document:
  content   = 题干（参与检索）
  metadata  = { answer: "A", explanation: "因为..." }  ← 结构化存储
```

- 检索命中题目后，追问"答案？""解析？"由 LLM 读上下文里的 metadata 回答，
  不需要再次检索；
- 要求"解析原文直出、LLM 不许改写"时，工具返回
  `ToolResult(direct_to_client=True)`，react 循环跳过 LLM 直接透传。

### 字段拆分的归属

blocks → 题干/答案/解析 的拆分（正则或 LLM）是**业务层代码**，
放在具体 Agent 项目（如 `my-cesium-agent-vanilla` 的 services），
Core 不含题库语义。通用格式（`数字.题干 + ABCD 选项 + 答案：X + 解析：...`）
可先用正则，真实 PDF 样例到手后再调准。

## PDF 解析：MinerU 适配器

`agent-core/packages/mineru/` 是独立安装包，包含
`MinerUBlobParser` / `MinerUDocumentLoader` / `HttpxMinerUTransport`，
通过 HTTP 调用外部 MinerU 服务（默认 `http://127.0.0.1:8000/file_parse`）。

关键认知：

- MinerU **不是聊天大模型**，是文档解析流水线（版面分析 + OCR + 公式识别 +
  表格识别多个小模型的组合），输入 PDF 输出 Markdown/JSON，只做忠实转换；
- 开源免费，但需自部署（完整能力建议 GPU）；
- 当前状态：适配器代码齐全，**服务未部署、包未安装、未在运行时接线**。

## 升级点（2026-11-02 已全部完成）

| 优先级 | 升级点 | 实现方式 |
| --- | --- | --- |
| P1 | checksum 防篡改 | `_verify_content_checksum`：Document / DocumentChunk 构造与 from_dict 统一校验，非空严格校验、为空自动计算 |
| P1 | 去重统一入口 | 新增 `dedup.py`：`deduplicate_documents` / `deduplicate_chunks`，按 checksum 顺序保持去重 |
| P2 | lazy_load 真流式 | `TextLoader` 新增有状态 `_StreamingTextReader`：按 `batch_chars` 分批读取、增量解码防中文乱码、换行优先切批、locator 记录全文连续字符区间 |
| P2 | metadata 最小 schema | 新增 `metadata.py`：`DocumentMetadataKeys` 保留键常量（source_checksum / loader / encoding / part_index 等），Loader 统一打标 |
| P2 | 解析器注册表 | 新增 `parsers.py`：`ParserRegistry` 按 MIME（含 `text/*` 通配）→ 扩展名匹配解析器，`default_parser_registry` 内置文本解析；MinerU 适配器可注册 `application/pdf` |
| P3 | 切分统计 | `SplitStats` + `split_documents_with_stats()`：chunk 数、平均/最大长度、达到上限的硬切块数 |

新增模块导出：`deduplicate_documents`、`deduplicate_chunks`、`DocumentMetadataKeys`、
`KNOWN_METADATA_KEYS`、`ParserRegistry`、`ParserNotFoundError`、`default_parser_registry`、`SplitStats`。

## 使用示例

```python
from agent_core.documents import TextLoader, RecursiveCharacterTextSplitter

# 1. 加载
docs = await TextLoader("题库.txt").load()

# 2. 切分
splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)
chunks = splitter.split_documents(docs)

# 3. 送入检索索引
for chunk in chunks:
    print(chunk.parent_document_id, chunk.chunk_index, chunk.locator.start_offset)
```

