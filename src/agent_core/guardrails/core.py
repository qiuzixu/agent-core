"""与业务无关的输入、输出安全校验中间件。

模块内含两类角色：
1. Guard：单条文本校验规则（禁词 / 长度 / PII / 注入内容），可独立复用；
2. GuardrailsMiddleware：把 Guard 接入模型调用前后，输入不合规拦截、输出不合规拦截，
   并通过 ctx.emit 上报可观测事件（对齐 compaction 中间件的上报模式）。

与 middleware/injection_guard.py 的分工：
- 本模块做"内容合规"（禁词、长度、PII、脚本/SQL 注入片段）；
- InjectionGuardMiddleware 做"指令注入"（Prompt Injection 模式库 + 工具面检测）。
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)


class Guard(ABC):
    """单个文本校验规则。"""

    @abstractmethod
    def check(self, text: str) -> tuple[bool, str]:
        """返回 ``(是否通过, 原因)``。"""

    @property
    def name(self) -> str:
        return self.__class__.__name__


class BlockedKeywordsGuard(Guard):
    """屏蔽指定关键词，默认大小写不敏感。

    支持热更新：配置中心 on_change 回调可调用 update_keywords() 动态下发新禁词表，
    无需重启服务。"""

    def __init__(self, keywords: Sequence[str] = (), case_sensitive: bool = False) -> None:
        self._flags = 0 if case_sensitive else re.IGNORECASE
        self._keywords = list(keywords)
        self._patterns = [re.compile(re.escape(keyword), self._flags) for keyword in self._keywords]

    def update_keywords(self, keywords: Sequence[str]) -> None:
        """热更新禁词表并重新编译正则。"""
        self._keywords = list(keywords)
        self._patterns = [re.compile(re.escape(keyword), self._flags) for keyword in self._keywords]

    def check(self, text: str) -> tuple[bool, str]:
        for pattern, keyword in zip(self._patterns, self._keywords, strict=True):
            if pattern.search(text):
                return False, f"输入包含违禁词：{keyword}"
        return True, ""


class LengthGuard(Guard):
    """限制输入或输出字符数。"""

    def __init__(
        self,
        max_input_chars: int | None = 5000,
        max_output_chars: int | None = None,
    ) -> None:
        self._max_input = max_input_chars
        self._max_output = max_output_chars

    def check(self, text: str) -> tuple[bool, str]:
        limit = self._max_input or self._max_output
        if limit and len(text) > limit:
            return False, f"文本长度 {len(text)} 超出限制 {limit}"
        return True, ""

    def check_input(self, text: str) -> tuple[bool, str]:
        if self._max_input and len(text) > self._max_input:
            return False, f"输入长度 {len(text)} 超出限制 {self._max_input}"
        return True, ""

    def check_output(self, text: str) -> tuple[bool, str]:
        if self._max_output and len(text) > self._max_output:
            return False, f"输出长度 {len(text)} 超出限制 {self._max_output}"
        return True, ""


class PIIRedactionGuard(Guard):
    """检测并脱敏常见手机号、身份证、邮箱和银行卡。

    银行卡额外要求通过 Luhn 校验位算法：纯 16~19 位数字（订单号、坐标串等）
    不再误报为银行卡。默认检测到仅脱敏放行，block_on_detection=True 时拦截。"""

    _PATTERNS: ClassVar[list[tuple[str, re.Pattern[str], str]]] = [
        ("手机号", re.compile(r"\b1[3-9]\d{9}\b"), "***手机号***"),
        ("身份证", re.compile(r"\b\d{17}[\dXx]\b"), "***身份证***"),
        ("邮箱", re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I), "***邮箱***"),
        ("银行卡", re.compile(r"\b\d{16,19}\b"), "***银行卡***"),
    ]

    def __init__(self, block_on_detection: bool = False) -> None:
        self._block = block_on_detection

    @staticmethod
    def _luhn_valid(digits: str) -> bool:
        """Luhn 校验位算法（ISO/IEC 7812-1），银行卡号必须通过。"""
        total = 0
        parity = len(digits) % 2
        for i, ch in enumerate(digits):
            digit = ord(ch) - 48
            if i % 2 == parity:
                digit *= 2
                if digit > 9:
                    digit -= 9
            total += digit
        return total % 10 == 0

    def _bank_card_matches(self, text: str) -> list[str]:
        """返回文本中通过 Luhn 校验的银行卡号（未通过的不算命中）。"""
        pattern = self._PATTERNS[3][1]
        return [m.group() for m in pattern.finditer(text) if self._luhn_valid(m.group())]

    def check(self, text: str) -> tuple[bool, str]:
        detected: list[str] = []
        for name, pattern, _ in self._PATTERNS:
            if name == "银行卡":
                if self._bank_card_matches(text):
                    detected.append(name)
            elif pattern.search(text):
                detected.append(name)
        if not detected:
            return True, ""
        if self._block:
            return False, f"输入/输出包含 PII：{', '.join(detected)}"
        logger.warning("[PII] 检测到 PII 类型：%s（已脱敏）", detected)
        return True, f"PII detected: {detected}"

    def redact(self, text: str) -> str:
        """脱敏所有命中的 PII；银行卡只在 Luhn 通过时替换，避免误伤普通长数字。"""
        for name, pattern, placeholder in self._PATTERNS:
            if name == "银行卡":
                text = pattern.sub(
                    lambda m: placeholder if self._luhn_valid(m.group()) else m.group(),
                    text,
                )
            else:
                text = pattern.sub(placeholder, text)
        return text


class OutputFormatGuard(Guard):
    """拦截常见脚本、SQL 注入和 JavaScript 协议内容。

    mode="block"（默认）：check() 命中即拦截整条输出，适合落库/渲染前的强把关；
    mode="redact"：check() 仍报告命中，但中间件改走 sanitize() 只把可疑片段
    替换为占位符，不拦截整条输出——避免用户正常讨论 SQL/前端知识被误杀。"""

    _REDACT_PLACEHOLDER: ClassVar[str] = "[已过滤]"

    _INJECTION_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"(DROP\s+TABLE|DELETE\s+FROM|INSERT\s+INTO)", re.I),
        re.compile(r"(<script[\s>])", re.I),
        re.compile(r"(javascript\s*:)", re.I),
    ]

    def __init__(self, mode: str = "block") -> None:
        if mode not in ("block", "redact"):
            raise ValueError(f"未知的 OutputFormatGuard 模式：{mode}")
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    def check(self, text: str) -> tuple[bool, str]:
        if any(pattern.search(text) for pattern in self._INJECTION_PATTERNS):
            return False, "输出包含潜在注入内容"
        return True, ""

    def sanitize(self, text: str) -> str:
        """把命中注入模式的片段替换为占位符；redact 模式下中间件调用本方法。"""
        result = text
        for pattern in self._INJECTION_PATTERNS:
            result = pattern.sub(self._REDACT_PLACEHOLDER, result)
        return result


class GuardrailsMiddleware(Middleware):
    """在模型调用前检查用户输入、调用后检查模型输出。

    可观测性：所有拦截/脱敏动作都会通过 ctx.emit 上报事件
    （guard_input_blocked / guard_output_blocked / guard_pii_redacted / guard_output_sanitized），
    与审计、tracing 中间件落同一事件序列，出事故可回查。"""

    def __init__(
        self,
        input_guards: Sequence[Guard] = (),
        output_guards: Sequence[Guard] = (),
        pii_redact: bool = False,
        output_sanitize: bool = False,
        emit: Callable[..., Any] | None = None,
    ) -> None:
        self._input_guards = list(input_guards)
        self._output_guards = list(output_guards)
        self._pii_redact = pii_redact
        # output_sanitize=True 时，OutputFormatGuard 命中走脱敏而非拦截（需注入 emit 才可观测）。
        self._output_sanitize = output_sanitize
        self._emit_hook = emit
        self._pii_guard = PIIRedactionGuard() if pii_redact else None

    def _emit(self, ctx: MiddlewareContext, event_type: str, **payload: Any) -> None:
        """上报可观测事件；上报本身出问题不影响主流程。"""
        try:
            if ctx.emit is not None:
                ctx.emit(event_type, **payload)
            elif self._emit_hook is not None:
                self._emit_hook(event_type, payload)
        except Exception as exc:
            logger.debug("[Guard] 事件上报失败：%s", exc)

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        user_text = next(
            (message.content for message in reversed(context.messages) if message.role == "user"),
            "",
        )
        if not user_text:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        for guard in self._input_guards:
            passed, reason = (
                guard.check_input(user_text) if isinstance(guard, LengthGuard) else guard.check(user_text)
            )
            if not passed:
                logger.warning("[Guard] 输入被拦截 [%s]: %s", guard.name, reason)
                self._emit(
                    context, "guard_input_blocked", guard=guard.name, reason=reason
                )
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    data={"guard": guard.name, "reason": reason},
                    error=reason,
                )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if context.llm_response is None:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        output_text = context.llm_response.content
        if self._pii_redact and self._pii_guard and output_text:
            redacted = self._pii_guard.redact(output_text)
            if redacted != output_text:
                logger.info("[Guard] 输出 PII 已脱敏")
                self._emit(context, "guard_pii_redacted")
                context.llm_response.content = redacted
                output_text = redacted
        for guard in self._output_guards:
            # redact 模式：命中只替换可疑片段，不拦截整条输出。
            if (
                self._output_sanitize
                and isinstance(guard, OutputFormatGuard)
                and guard.mode == "redact"
            ):
                sanitized = guard.sanitize(output_text)
                if sanitized != output_text:
                    logger.warning("[Guard] 输出包含注入片段，已脱敏放行 [%s]", guard.name)
                    self._emit(
                        context,
                        "guard_output_sanitized",
                        guard=guard.name,
                    )
                    context.llm_response.content = sanitized
                    output_text = sanitized
                continue
            passed, reason = (
                guard.check_output(output_text) if isinstance(guard, LengthGuard) else guard.check(output_text)
            )
            if not passed:
                logger.warning("[Guard] 输出被拦截 [%s]: %s", guard.name, reason)
                self._emit(
                    context, "guard_output_blocked", guard=guard.name, reason=reason
                )
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    data={"guard": guard.name, "reason": reason},
                    error=reason,
                )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)


__all__ = [
    "BlockedKeywordsGuard",
    "Guard",
    "GuardrailsMiddleware",
    "LengthGuard",
    "OutputFormatGuard",
    "PIIRedactionGuard",
]