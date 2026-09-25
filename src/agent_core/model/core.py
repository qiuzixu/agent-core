"""模型调用协议。

Agent Core 只依赖该协议，不依赖 OpenAI、Anthropic、Gemini 或其他模型 SDK。
具体模型适配器由 ``agent_core.model.providers`` 提供，应用也可以注册自定义适配器。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from agent_core.protocol.messages import Message


@dataclass
class StreamChunk:
    """模型流式输出的统一数据块。"""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    is_tool_call: bool = False
    finish_reason: str | None = None


@dataclass(frozen=True)
class ContextUsage:
    """模型返回的上下文使用量。

    ``exact`` 用于区分提供商真实计数与本地估算；Core 不规定计数实现，
    由具体模型适配器决定如何填充。
    """

    input_tokens: int | None
    context_window_tokens: int | None
    exact: bool
    source: str


@runtime_checkable
class ModelAdapter(Protocol):
    """Agent Loop 使用的最小模型协议。"""

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message: ...

    def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]: ...

    async def context_usage(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ContextUsage: ...


# 模型提供商构造器。Core 不关心配置对象的具体类型，应用可以传入
# Pydantic Settings、字典或自己的配置类。
ModelProviderFactory = Callable[..., ModelAdapter]


class ModelProviderRegistry:
    """可注册的模型提供商工厂。

    Core 管理名称到构造器的映射，并内置 OpenAI、Anthropic、Ollama、Gemini
    适配器；应用可以继续注册自己的适配器。具体 SDK 由 Provider 延迟导入。
    """

    def __init__(self) -> None:
        self._factories: dict[str, ModelProviderFactory] = {}

    def register(
        self,
        name: str,
        factory: ModelProviderFactory,
        *,
        replace: bool = False,
    ) -> None:
        """注册一个模型提供商构造器。"""
        normalized = name.strip().lower()
        if not normalized:
            raise ValueError("模型提供商名称不能为空")
        if normalized in self._factories and not replace:
            raise ValueError(f"模型提供商已注册：{normalized}")
        self._factories[normalized] = factory

    def unregister(self, name: str) -> None:
        """移除一个已注册的提供商。"""
        self._factories.pop(name.strip().lower(), None)

    def names(self) -> tuple[str, ...]:
        """返回已注册的提供商名称。"""
        return tuple(sorted(self._factories))

    def create(
        self,
        name: str,
        config: Any,
        *,
        model: str | None = None,
        **kwargs: Any,
    ) -> ModelAdapter:
        """根据提供商名称创建模型适配器。"""
        normalized = name.strip().lower()
        factory = self._factories.get(normalized)
        if factory is None:
            available = ", ".join(self.names()) or "无"
            raise ValueError(
                f"不支持的模型提供商：{normalized or '<empty>'}；已注册：{available}"
            )
        return factory(config, model=model, **kwargs)


default_model_provider_registry = ModelProviderRegistry()


def register_model_provider(
    name: str,
    factory: ModelProviderFactory,
    *,
    replace: bool = False,
    registry: ModelProviderRegistry | None = None,
) -> None:
    """向默认或指定注册表注册模型提供商。"""
    (registry or default_model_provider_registry).register(
        name,
        factory,
        replace=replace,
    )


def create_model_provider(
    config: Any,
    *,
    registry: ModelProviderRegistry | None = None,
    provider: str | None = None,
    model: str | None = None,
    **kwargs: Any,
) -> ModelAdapter:
    """使用注册表创建模型适配器。

    ``provider`` 省略时读取配置对象的 ``llm_provider``，默认为 ``openai``。
    具体密钥校验和 SDK 初始化由对应的 Provider 工厂完成。
    """
    selected = provider or getattr(config, "llm_provider", None) or "openai"
    return (registry or default_model_provider_registry).create(
        str(selected),
        config,
        model=model,
        **kwargs,
    )


def build_tool_definition(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """构造通用的 function-calling 工具定义。"""

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
