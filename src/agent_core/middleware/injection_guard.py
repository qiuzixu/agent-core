"""Prompt Injection 增强防护中间件。

在 GuardrailsMiddleware 基础上增加：
1. 工具结果中的注入检测（工具返回的数据可能包含恶意指令）
2. 多轮上下文中的渐进式注入检测
3. 系统指令覆盖尝试检测
4. 角色混淆检测（"你现在是..."）

检测到注入时不直接 STOP（避免误杀），而是：
- 从消息中剥离可疑内容
- 在 metadata 中标记 warning
- 严格模式下 STOP

配置：
    AGENT_INJECTION_GUARD_ENABLED: 是否启用（默认 true）
    AGENT_INJECTION_GUARD_STRICT: 严格模式（默认 false）
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["InjectionGuardConfig", "InjectionGuardMiddleware"]


@dataclass
class InjectionGuardConfig:
    """注入防护配置。"""

    enabled: bool = True
    strict: bool = False
    # 额外的自定义注入模式（正则）
    custom_patterns: list[re.Pattern[str]] = field(default_factory=list)


# 内置检测模式
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # 系统指令覆盖
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)", re.IGNORECASE),
    re.compile(r"忽略(以上|之前|上面|所有)?(的)?(指令|提示|规则|约束)", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)", re.IGNORECASE),
    re.compile(r"forget\s+(everything|all|your)\s+(instructions?|training|rules?)", re.IGNORECASE),
    # 角色混淆
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
    re.compile(r"你(现在)?是(一个)?(新的|不同的)?(角色|AI|助手|系统)", re.IGNORECASE),
    re.compile(r"pretend\s+(to\s+be|you\s+are)", re.IGNORECASE),
    re.compile(r"act\s+as\s+(if|a|an)", re.IGNORECASE),
    # 越狱
    re.compile(r"(DAN|do\s+anything\s+now)\s+mode", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"developer\s+mode", re.IGNORECASE),
    re.compile(r"(越狱|绕过)(模式|限制|审查)?", re.IGNORECASE),
    # 输出系统提示
    re.compile(r"(print|show|repeat|output|reveal)\s+(your\s+)?(system\s+)?(prompt|instructions)", re.IGNORECASE),
    re.compile(r"(输出|打印|显示|泄露)(你的)?(系统)?(提示词|指令)", re.IGNORECASE),
    # 工具注入
    re.compile(r"\{\{.*\}\}"),  # 模板注入
    re.compile(r"\$\{.*\}"),    # 变量注入
]


class InjectionGuardMiddleware(Middleware):
    priority = 2
    """增强 Prompt Injection 防护。

    before_model: 检查用户消息中的注入尝试
    before_tool:  检查工具参数中的注入尝试
    after_tool:   检查工具结果中的注入尝试（工具返回的外部数据可能被污染）
    """

    def __init__(
        self,
        config: InjectionGuardConfig | None = None,
        emit: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config or InjectionGuardConfig()
        self._emit_hook = emit

    def set_custom_patterns(self, patterns: list[re.Pattern[str]]) -> None:
        """热更新自定义注入模式（配置中心 on_change 回调可调用）。"""
        self._config.custom_patterns = list(patterns)

    def _detect(self, text: str) -> list[str]:
        """检测文本中的注入模式。返回匹配到的模式描述列表。"""
        if not text:
            return []
        detected = []
        for pattern in self._config.custom_patterns:
            if pattern.search(text):
                detected.append(f"custom: {pattern.pattern[:50]}")
        for pattern in _INJECTION_PATTERNS:
            match = pattern.search(text)
            if match:
                detected.append(f"builtin: '{match.group()[:60]}'")
        return detected

    def _sanitize(self, text: str) -> str:
        """从文本中剥离可疑内容（替换为 [FILTERED]）。"""
        result = text
        for pattern in [*self._config.custom_patterns, *_INJECTION_PATTERNS]:
            result = pattern.sub("[FILTERED]", result)
        return result

    def _emit(self, ctx: MiddlewareContext, event_type: str, **payload: Any) -> None:
        """上报可观测事件；上报本身出问题不影响主流程（对齐 compaction 模式）。"""
        try:
            if ctx.emit is not None:
                ctx.emit(event_type, **payload)
            elif self._emit_hook is not None:
                self._emit_hook(event_type, payload)
        except Exception as exc:
            logger.debug("[InjectionGuard] 事件上报失败：%s", exc)

    def _handle_detection(
        self, context: MiddlewareContext, source: str, detections: list[str]
    ) -> MiddlewareResult:
        """处理检测到的注入。"""
        warning = f"[InjectionGuard] {source} 检测到 {len(detections)} 个注入模式: {detections[:3]}"
        logger.warning(warning)
        context.metadata.setdefault("injection_warnings", []).extend(
            [{"source": source, "patterns": detections}]
        )
        self._emit(
            context,
            "injection_detected",
            source=source,
            count=len(detections),
            samples=detections[:3],
            strict=self._config.strict,
        )
        if self._config.strict:
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error=f"检测到疑似 Prompt Injection ({source})，已阻止",
            )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 检查最新的用户消息
        for msg in reversed(context.messages):
            role = getattr(msg, "role", "")
            if role in ("user", "human"):
                content = str(getattr(msg, "content", "") or "")
                detections = self._detect(content)
                if detections:
                    return self._handle_detection(context, "user_message", detections)
                break
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        response = context.llm_response
        content = str(getattr(response, "content", "") or "") if response is not None else ""
        if not content:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        detections = self._detect(content)
        if not detections:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 模型输出可能复述工具结果里的注入指令；宽松模式剥离可疑片段后放行，
        # 严格模式直接拦截整轮输出。
        logger.warning(
            "[InjectionGuard] model_output 检测到 %d 个注入模式: %s",
            len(detections),
            detections[:3],
        )
        context.metadata.setdefault("injection_warnings", []).extend(
            [{"source": "model_output", "patterns": detections}]
        )
        self._emit(
            context,
            "injection_detected",
            source="model_output",
            count=len(detections),
            samples=detections[:3],
            strict=self._config.strict,
        )
        if self._config.strict:
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                error="检测到疑似 Prompt Injection (model_output)，已阻断",
            )
        sanitized = self._sanitize(content)
        if sanitized != content and response is not None:
            response.content = sanitized
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 检查工具参数
        args_str = str(context.tool_args or "")
        detections = self._detect(args_str)
        if detections:
            return self._handle_detection(context, f"tool_args({context.tool_name})", detections)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 检查工具返回结果（外部数据可能包含注入）
        result = context.tool_result or ""
        detections = self._detect(str(result))
        if detections:
            return self._handle_detection(context, f"tool_result({context.tool_name})", detections)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
