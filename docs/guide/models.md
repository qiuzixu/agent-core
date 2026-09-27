# 模型与适配器

Agent Core 通过 `ModelAdapter` 隔离模型厂商差异。内置适配器支持 OpenAI、Anthropic、Gemini
和 Ollama；兼容 OpenAI API 的服务可以复用 `OpenAIProvider`。

## 安装可选依赖

```bash
pip install "handwritten-agent-core[openai]"
pip install "handwritten-agent-core[anthropic]"
pip install "handwritten-agent-core[gemini]"
pip install "handwritten-agent-core[models]"
```

Ollama 使用 OpenAI 兼容客户端，因此安装 `openai` extra。`qwen` extra 提供 DashScope SDK 和
Qwen tokenizer；通过 DashScope 的 OpenAI 兼容地址调用时，Provider 仍选择 `openai`。

## 统一工厂

`create_model_provider()` 默认读取配置对象的 `llm_provider`，并允许本次请求覆盖 Provider 或模型：

```python
model = create_model_provider(settings)
model = create_model_provider(settings, provider="gemini", model="gemini-2.5-flash")
```

工厂读取的公共属性见[配置参考](../reference/configuration.md)。Core 不读取 `.env`，应用负责把
环境变量、配置中心或数据库记录转换成配置对象。

## 直接构造 Provider

```python
from agent_core import OpenAIProvider

model = OpenAIProvider(
    api_key="从安全配置读取",
    model="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
    temperature=0.0,
    max_tokens=4096,
)
```

## 注册自定义 Provider

构造器接受配置对象和可选 `model`，返回一个满足 `ModelAdapter` 的对象：

```python
from agent_core import register_model_provider


def build_private_model(config, *, model=None, **kwargs):
    return PrivateModel(
        endpoint=config.private_endpoint,
        model=model or config.model_name,
    )


register_model_provider("private", build_private_model)
```

如果名称已经存在，默认拒绝覆盖；明确需要替换时传 `replace=True`。完整离线实现见
`examples/custom_model.py`。

## 上下文用量

`context_usage()` 返回输入 token、上下文窗口、是否为精确值和数据来源。
`CompactionMiddleware` 优先使用 Provider 的真实计数，Provider 不支持时可退回本地估算。
为私有模型实现准确的用量接口，可以显著改善压缩触发时机。

## 结构化输出

`chat_structured()` 用统一 Prompt 约束模型输出 JSON，并在 Core 内完成 JSON Schema 子集校验。
因此内置和自定义 Provider 都能获得相同语义，不要求厂商提供原生 JSON Schema 接口。

```python
from agent_core import StructuredOutputSpec, chat_structured, user_message

spec = StructuredOutputSpec[dict](
    name="flight_plan",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "altitude": {"type": "integer", "minimum": 0},
        },
        "required": ["name", "altitude"],
        "additionalProperties": False,
    },
    max_retries=1,
)
result = await chat_structured(model, [user_message("生成飞行计划")], spec)
print(result.value, result.attempts)
```

支持 `type`、`required`、`properties`、`additionalProperties`、`items`、`enum`、字符串长度和
数值范围。需要转换为 dataclass 或增加业务校验时，给 `decoder` 传入函数。校验失败时，Core 会把
错误反馈给模型并在 `max_retries` 范围内重试。

## 限流、熔断和 fallback

`GovernedModelAdapter` 包装任意 `ModelAdapter`，增加按 scope 隔离的并发、滑动窗口限流、调用
超时、熔断和候选模型降级：

```python
from agent_core import GovernedModelAdapter, ModelExecutionPolicy, model_execution_scope

governed = GovernedModelAdapter(
    primary_model,
    fallbacks=[backup_model],
    policy=ModelExecutionPolicy(
        max_concurrency=4,
        requests_per_window=60,
        tokens_per_window=120_000,
        call_timeout_seconds=45,
        failure_threshold=3,
    ),
)

with model_execution_scope("tenant-a"):
    response = await governed.chat(messages)
```

Token 限制当前统计估算输入量，因为 `ModelAdapter` 尚未统一返回输出 usage。流式调用只会在尚未
输出 chunk 时切换 fallback；一旦已向调用方输出内容，后续异常会直接抛出，避免重复文本。

## 模型目录与选择

`default_model_catalog()` 提供可展示的模型元数据，`catalog_payload()` 会输出适合 API 返回的
脱敏结构。`ModelSelectionStore` 保存用户或租户选择；它不保存 API Key。应用应校验用户选择
是否在允许目录中，再把 Provider 和模型传给工厂。
