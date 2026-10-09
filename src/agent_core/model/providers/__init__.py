"""多模型切换：Protocol 抽象 + 各提供商适配器。

使用方式：
    # 开发环境（OpenAI 兼容接口）
    llm = OpenAIProvider(api_key="sk-...", model="gpt-4o-mini")

    # Anthropic Claude
    llm = AnthropicProvider(api_key="sk-ant-...", model="claude-3-5-sonnet-20241022")

    # 本地 Ollama
    llm = OllamaProvider(model="qwen2.5:7b")

    # 从环境变量/配置自动选择
    llm = create_llm_provider(config)

所有提供商均实现 ``ModelAdapter`` 协议，ReActAgent 只依赖该协议，
切换提供商无需修改任何 Agent 代码。

模块结构：
    common.py             共享辅助（已知上下文窗口规格、统一 logger）
    openai_compatible.py  OpenAI 兼容接口适配器（DashScope/Qwen 复用该实现）
    anthropic.py          Anthropic Claude 适配器
    gemini.py             Google Gemini 适配器
    ollama.py             Ollama 本地模型适配器（复用 OpenAI 兼容接口）
    factory.py            提供商构造器、注册与 create_llm_provider 工厂入口
"""

from agent_core.model.providers.anthropic import AnthropicProvider
from agent_core.model.providers.common import known_context_window
from agent_core.model.providers.factory import MODEL_PROVIDER_REGISTRY, create_llm_provider
from agent_core.model.providers.gemini import GeminiProvider
from agent_core.model.providers.ollama import OllamaProvider
from agent_core.model.providers.openai_compatible import OpenAIProvider

__all__ = [
    "MODEL_PROVIDER_REGISTRY",
    "AnthropicProvider",
    "GeminiProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "create_llm_provider",
    "known_context_window",
]
