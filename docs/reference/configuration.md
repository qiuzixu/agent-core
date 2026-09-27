# 配置参考

Core 不绑定配置框架，也不自动读取 `.env`。工厂通过属性访问读取应用传入的配置对象。
下面的环境变量名是应用层推荐映射，不是 Core 自动解析行为。

## 模型配置

| 配置属性 | 推荐环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `llm_provider` | `AGENT_LLM_PROVIDER` | `openai` | Provider 注册名 |
| `model_name` | `AGENT_MODEL_NAME` | 空 | 模型 ID |
| `temperature` | `AGENT_TEMPERATURE` | `0.0` | 生成温度 |
| `max_tokens` | `AGENT_MAX_TOKENS` | `4096` | 最大输出 token |
| `context_window_tokens` | `AGENT_CONTEXT_WINDOW_TOKENS` | 自动/未知 | 手动覆盖窗口大小 |
| `openai_api_key` | `AGENT_OPENAI_API_KEY` | 无 | OpenAI/兼容接口密钥 |
| `openai_base_url` | `AGENT_OPENAI_BASE_URL` | OpenAI 官方地址 | 兼容接口地址 |
| `anthropic_api_key` | `AGENT_ANTHROPIC_API_KEY` | 无 | Anthropic 密钥 |
| `gemini_api_key` | `AGENT_GEMINI_API_KEY` | 无 | Gemini 密钥 |
| `ollama_base_url` | `AGENT_OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama 兼容地址 |

## 模型调用治理

`ModelExecutionPolicy` 由应用显式传给 `GovernedModelAdapter`，不从环境变量自动读取：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `max_concurrency` | `8` | 每个 scope 的最大并发模型调用数 |
| `requests_per_window` | `None` | 窗口内最大请求数，`None` 表示不限制 |
| `tokens_per_window` | `None` | 窗口内最大估算输入 Token，`None` 表示不限制 |
| `window_seconds` | `60` | 滑动窗口秒数 |
| `queue_timeout_seconds` | `30` | 等待限流或并发名额的最长时间 |
| `call_timeout_seconds` | `None` | 单次模型调用超时 |
| `failure_threshold` | `5` | 连续失败多少次后打开熔断器 |
| `recovery_timeout_seconds` | `30` | 熔断后多久允许恢复探测 |

应用可用 `model_execution_scope(tenant_id)` 把配额隔离到租户或用户。未设置时使用 `default`。

## Agent Loop

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `max_iterations` | `5` | 一次 Run 的最大模型轮数 |
| `max_tool_calls` | `20` | 一次 Run 的最大工具调用数 |
| `thread_id` | `default` | 未显式传入时的线程 ID |

## Runtime

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `worker_id` | 自动生成 | 当前 Worker 标识 |
| `lease_seconds` | `30` | Run 租约时间 |
| `heartbeat_seconds` | `10` | 租约心跳间隔 |
| `require_access` | `False` | 是否强制用户和租户身份 |

`heartbeat_seconds` 必须明显小于 `lease_seconds`，并根据事件循环延迟和数据库抖动保留余量。

## 存储工厂

所有工厂接受 `env`、`sqlite_path` 和 `postgres_url`；Session、Context 和长期 Memory 工厂额外
接受 `require_access`。生产环境应把数据库 URL 放在密钥管理系统中。

```python
store = create_runtime_store(
    env="production",
    postgres_url="从安全配置读取",
)
```

长期记忆使用独立工厂：

```python
memory = create_memory_store(
    env="production",
    postgres_url="从安全配置读取",
    require_access=True,
)
await memory.initialize()  # PostgreSQL 实现需要初始化连接池和表
```

## Embedding Provider 扩展包

`handwritten-agent-core-embeddings` 不读取环境变量，应用可以按以下名称映射配置：

| 配置属性 | 推荐环境变量 | 常用默认值 | 说明 |
| --- | --- | --- | --- |
| `provider` | `AGENT_EMBEDDING_PROVIDER` | 无 | `openai`、`gemini` 或 `ollama` |
| `model` | `AGENT_EMBEDDING_MODEL` | Provider 默认值 | Embedding 模型 ID |
| `api_key` | `AGENT_EMBEDDING_API_KEY` | 无 | OpenAI/Gemini 或受保护 Ollama 的密钥 |
| `base_url` | `AGENT_EMBEDDING_BASE_URL` | Provider 默认值 | OpenAI 兼容地址或 Ollama 地址 |
| `dimensions` | `AGENT_EMBEDDING_DIMENSIONS` | 模型默认值 | OpenAI/Gemini 请求维度 |

`EmbeddingExecutionPolicy` 由应用显式构造：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `batch_size` | `64` | 每次 Provider 请求的文本数 |
| `max_concurrency` | `2` | 同时执行的批次数 |
| `timeout_seconds` | `60` | 单批调用超时 |
| `max_retries` | `2` | 失败后的最大重试次数 |
| `retry_base_seconds` | `0.5` | 指数退避起始秒数 |
| `expected_dimensions` | `None` | 强制校验返回向量维度 |

索引和查询必须使用相同模型与维度。切换模型或维度后，应使用新的 namespace/表并重建索引。

## 向量存储

向量存储工厂使用单独参数，且允许显式 `backend` 覆盖环境默认值：

| 环境 | 默认后端 | 必要参数 |
| --- | --- | --- |
| `test` / `testing` | `memory` | 无 |
| 开发环境 | `chroma` | `chroma_path`，默认 `./chroma_db` |
| `production` / `prod` | `pgvector` | `postgres_url`、`dimension` |

```python
vectors = create_vector_store(
    env="production",
    postgres_url="从安全配置读取",
    dimension=1536,
    collection_name="flight-manual",
    require_access=True,
)
await vectors.initialize()
```

推荐由应用映射 `AGENT_VECTOR_STORE`、`AGENT_VECTOR_PATH`、
`AGENT_VECTOR_DIMENSION` 和数据库 URL。数据库管理员已安装 `vector` 扩展或已创建索引时，
可传 `pg_create_extension=False` 或 `pg_create_index=False`。同一 pgvector 表只保存一种维度；
切换 Embedding 维度时应更换 `table_name` 并重建索引。

## MinerU 扩展包

独立包 `handwritten-agent-core-mineru` 的 `MinerUConfig` 只配置自托管 MinerU `/file_parse`
HTTP 调用。Core 与扩展包都不自动读取环境变量，应用可以按下表映射配置：

| 配置属性 | 推荐环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `endpoint` | `AGENT_MINERU_ENDPOINT` | `http://127.0.0.1:8000/file_parse` | 自托管解析地址 |
| `api_key` | `AGENT_MINERU_API_KEY` | 无 | 可选 Bearer Token |
| `timeout_seconds` | `AGENT_MINERU_TIMEOUT` | `300` | 单次解析超时秒数 |
| `backend` | `AGENT_MINERU_BACKEND` | `pipeline` | MinerU 解析后端 |
| `parse_method` | `AGENT_MINERU_PARSE_METHOD` | `auto` | 解析方式 |
| `language` | `AGENT_MINERU_LANGUAGE` | `ch` | 文档语言 |
| `output_mode` | `AGENT_MINERU_OUTPUT_MODE` | `blocks` | 返回区块或完整 Markdown |
| `max_file_bytes` | `AGENT_MINERU_MAX_FILE_BYTES` | `104857600` | 客户端文件大小上限 |

API Key 应由应用从密钥管理系统读取后传入。MinerU 云端异步 API 不使用这组配置，应实现自定义
`MinerUTransport`。

## Multi-Agent 扩展包

`CoordinationPolicy` 由应用显式构造，扩展包不自动读取环境变量：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `max_handoffs` | `8` | 一个协调实例最多交接次数 |
| `max_agent_calls` | `16` | 包含重试的子 Agent 调用总数 |
| `max_visits_per_agent` | `2` | 防止 Agent 之间无限循环 |
| `max_agent_attempts` | `1` | 单个子任务最大尝试次数 |
| `agent_timeout_seconds` | `120` | 单次 Agent 调用超时 |
| `retry_base_seconds` | `0.5` | 指数退避起始秒数 |
| `max_parallelism` | `4` | `run_parallel()` 全局并行数 |
| `max_total_tokens` | `None` | 可选协调实例 Token 预算 |
| `max_total_cost` | `None` | 可选协调实例费用预算 |
| `require_access` | `False` | 是否强制要求 `AccessContext` |
| `lease_seconds` | `30` | 多 Worker 租约有效期 |
| `heartbeat_seconds` | `10` | 租约续期频率，必须小于有效期 |

推荐由应用映射 `AGENT_MULTI_MAX_HANDOFFS`、`AGENT_MULTI_MAX_CALLS`、
`AGENT_MULTI_AGENT_TIMEOUT`、`AGENT_MULTI_MAX_PARALLELISM` 和租约配置。测试、开发和生产分别把
`WorkflowCoordinationStore` 接到 Memory、SQLite、PostgreSQL Workflow Store；多 Worker 环境同时
注入同后端的 `RunLeaseStore`。

## 压缩

`CompactionConfig` 主要参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `threshold_ratio` | `0.75` | 达到窗口比例后压缩 |
| `retain_ratio` | `0.20` | 保留近期原文占窗口比例 |
| `summary_max_tokens` | `2048` | 摘要最大输出 |
| `max_retries` | `1` | 常规额外压缩次数 |
| `max_overflow_retries` | `1` | 上下文超长后的强制重试次数 |
| `enabled` | `True` | 总开关 |
| `chars_per_token` | `4` | 无精确计数时的估算比例 |

## MkDocs 文档

文档默认部署在站点根路径。子路径部署时设置 `DOCS_BASE`：

```bash
DOCS_BASE=/handwritten-agent-core/ python -m mkdocs build
```

Windows PowerShell：

```powershell
$env:DOCS_BASE = "/handwritten-agent-core/"
python -m mkdocs build
```
