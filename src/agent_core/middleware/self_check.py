"""Agent Self-Check：最终回答前的规则自检。

after_model 阶段检查 LLM 输出是否满足基本质量规则：
- not_empty：回答不为空
- no_apology_only：回答不能只有"抱歉/对不起"
- no_raw_error：不包含原始 traceback / 裸 API 错误
- min_length：回答长度下限

自检失败时返回 STOP，上层可据此重试或降级。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

_APOLOGY_PATTERN = re.compile(r"^(?:抱歉|对不起|很抱歉|sorry)[，,。!！.\s]*$", re.IGNORECASE)
_TRACEBACK_PATTERN = re.compile(r"Traceback \(most recent call last\)")
_API_ERROR_PATTERN = re.compile(r"(?:HTTP \d{3}|status code[: ]\d{3}|API [Ee]rror)")


@dataclass
class SelfCheckConfig:
    """自检规则配置。"""

    enabled: bool = True
    min_length: int = 1
    rules: frozenset[str] = field(
        default_factory=lambda: frozenset({"not_empty", "no_apology_only", "no_raw_error", "min_length"})
    )


def _get_tool_calls(response: Any) -> list:
    """兼容 dict / object 提取 tool_calls。"""
    if response is None:
        return []
    calls = getattr(response, "tool_calls", None)
    if calls is None and isinstance(response, dict):
        calls = response.get("tool_calls")
    return calls or []


def _get_content(response: Any) -> str:
    """兼容 dict / object 提取文本。"""
    if response is None:
        return ""
    content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    if isinstance(content, list):
        parts = [str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content]
        return "\\n".join(parts)
    return str(content or "")


def check_answer(response: Any, config: SelfCheckConfig) -> list[str]:
    """返回未通过的规则名称列表（空列表=全部通过）。"""
    if _get_tool_calls(response):
        return []  # 尚未到最终回答，跳过
    text = _get_content(response).strip()
    issues: list[str] = []
    if "not_empty" in config.rules and not text:
        issues.append("回答为空")
    if "no_apology_only" in config.rules and _APOLOGY_PATTERN.fullmatch(text):
        issues.append("回答仅为道歉")
    if "no_raw_error" in config.rules:
        if _TRACEBACK_PATTERN.search(text):
            issues.append("包含原始 traceback")
        elif _API_ERROR_PATTERN.search(text) and len(text) < 200:
            issues.append("包含未包装的 API 错误")
    if "min_length" in config.rules and len(text) < config.min_length:
        issues.append(f"回答过短（{len(text)} < {config.min_length}）")
    return issues


class SelfCheckMiddleware(Middleware):
    """Agent 层规则自检。"""

    def __init__(self, config: SelfCheckConfig | None = None) -> None:
        self._config = config or SelfCheckConfig()

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        issues = check_answer(context.llm_response, self._config)
        if issues:
            reason = "Self-Check 未通过: " + "; ".join(issues)
            logger.warning(reason)
            return MiddlewareResult(action=MiddlewareAction.STOP, error=reason)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


__all__ = ["SelfCheckConfig", "SelfCheckMiddleware", "check_answer"]
