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

## 模型目录与选择

`default_model_catalog()` 提供可展示的模型元数据，`catalog_payload()` 会输出适合 API 返回的
脱敏结构。`ModelSelectionStore` 保存用户或租户选择；它不保存 API Key。应用应校验用户选择
是否在允许目录中，再把 Provider 和模型传给工厂。
