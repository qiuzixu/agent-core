"""Provider 共享辅助：已公开模型规格与统一 logger。

具体厂商适配器见 ``openai_compatible``、``anthropic``、``gemini``、``ollama``；
工厂与注册逻辑见 ``factory``。
"""

from __future__ import annotations

import logging

# 保留拆分前单文件模块的 logger 名，日志过滤行为保持不变。
logger = logging.getLogger("agent_core.model.providers")


# ── token / 已公开模型规格辅助函数 ────────────────────────────────
def known_context_window(model: str) -> int | None:
    """已公开模型规格；未知模型必须由 provider metadata 或配置提供。"""
    normalized = model.lower()
    known_windows = {
        "gpt-4o": 128_000,
        "gpt-4o-mini": 128_000,
        "gpt-4.1": 1_047_576,
        "gpt-4.1-mini": 1_047_576,
        "gpt-4.1-nano": 1_047_576,
        "qwen3.7-plus": 1_000_000,
        "qwen3-max": 262_144,
    }
    return known_windows.get(normalized)
