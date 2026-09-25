"""模型目录和运行时选择协议。

目录只包含浏览器可以看到的模型标识和能力，不包含 API Key、地址或其他密钥。
具体应用可以传入自己的目录，模型工厂仍由 ``model.core`` 负责创建适配器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelOption:
    """一个可供用户选择的模型。"""

    provider: str
    model: str
    label: str
    context_window_tokens: int | None = None
    capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset({"chat", "stream", "tool_calling"})
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "label": self.label,
            "context_window_tokens": self.context_window_tokens,
            "capabilities": sorted(self.capabilities),
        }


def default_model_catalog() -> tuple[ModelOption, ...]:
    """返回不依赖第三方 SDK 的安全默认目录。"""
    return (
        ModelOption("openai", "gpt-4o-mini", "OpenAI · GPT-4o mini", 128_000),
        ModelOption("openai", "gpt-4.1-mini", "OpenAI · GPT-4.1 mini", 1_047_576),
        ModelOption("anthropic", "claude-3-5-sonnet-latest", "Anthropic · Claude Sonnet"),
        ModelOption("gemini", "gemini-2.0-flash", "Gemini · 2.0 Flash"),
        ModelOption("ollama", "qwen2.5:7b", "Ollama · Qwen 2.5 7B"),
    )


def catalog_payload(
    options: tuple[ModelOption, ...] | list[ModelOption] | None = None,
) -> list[dict[str, Any]]:
    """把模型目录转换为 API 可返回的 JSON。"""
    return [item.to_dict() for item in (options or default_model_catalog())]


__all__ = ["ModelOption", "catalog_payload", "default_model_catalog"]
