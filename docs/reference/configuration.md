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

所有工厂接受 `env`、`sqlite_path` 和 `postgres_url`；Session 和 Context 额外接受
`require_access`。生产环境应把数据库 URL 放在密钥管理系统中。

```python
store = create_runtime_store(
    env="production",
    postgres_url="从安全配置读取",
)
```

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
