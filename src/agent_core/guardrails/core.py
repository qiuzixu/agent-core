"""与业务无关的输入、输出安全校验中间件。"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from agent_core.middleware import Middleware, MiddlewareAction, MiddlewareContext, MiddlewareResult

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
    """屏蔽指定关键词，默认大小写不敏感。"""

    def __init__(self, keywords: list[str] = (), case_sensitive: bool = False) -> None:
        flags = 0 if case_sensitive else re.IGNORECASE
        self._keywords = list(keywords)
        self._patterns = [re.compile(re.escape(keyword), flags) for keyword in self._keywords]

    def check(self, text: str) -> tuple[bool, str]:
        for pattern, keyword in zip(self._patterns, self._keywords):
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
    """检测并脱敏常见手机号、身份证、邮箱和银行卡号。"""

    _PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
        ("手机号", re.compile(r"\b1[3-9]\d{9}\b"), "***手机号***"),
        ("身份证", re.compile(r"\b\d{17}[\dXx]\b"), "***身份证***"),
        ("邮箱", re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I), "***邮箱***"),
        ("银行卡", re.compile(r"\b\d{16,19}\b"), "***银行卡***"),
    ]

    def __init__(self, block_on_detection: bool = False) -> None:
        self._block = block_on_detection

    def check(self, text: str) -> tuple[bool, str]:
        detected = [name for name, pattern, _ in self._PATTERNS if pattern.search(text)]
        if not detected:
            return True, ""
        if self._block:
            return False, f"输入/输出包含 PII：{', '.join(detected)}"
        logger.warning("[PII] 检测到 PII 类型：%s（已脱敏）", detected)
        return True, f"PII detected: {detected}"

    def redact(self, text: str) -> str:
        for _, pattern, placeholder in self._PATTERNS:
            text = pattern.sub(placeholder, text)
        return text


class OutputFormatGuard(Guard):
    """拦截常见脚本、SQL 注入和 JavaScript 协议内容。"""

    _INJECTION_PATTERNS = [
        re.compile(r"(DROP\s+TABLE|DELETE\s+FROM|INSERT\s+INTO)", re.I),
        re.compile(r"(<script[\s>])", re.I),
        re.compile(r"(javascript\s*:)", re.I),
    ]

    def check(self, text: str) -> tuple[bool, str]:
        if any(pattern.search(text) for pattern in self._INJECTION_PATTERNS):
            return False, "输出包含潜在注入内容"
        return True, ""


class GuardrailsMiddleware(Middleware):
    """在模型调用前检查用户输入、调用后检查模型输出。"""

    def __init__(
        self,
        input_guards: list[Guard] = (),
        output_guards: list[Guard] = (),
        pii_redact: bool = False,
    ) -> None:
        self._input_guards = list(input_guards)
        self._output_guards = list(output_guards)
        self._pii_redact = pii_redact
        self._pii_guard = PIIRedactionGuard() if pii_redact else None

    async def before_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        user_text = next(
            (message.content for message in reversed(ctx.messages) if message.role == "user"),
            "",
        )
        if not user_text:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        for guard in self._input_guards:
            passed, reason = guard.check(user_text)
            if not passed:
                logger.warning("[Guard] 输入被拦截 [%s]: %s", guard.name, reason)
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    data={"guard": guard.name, "reason": reason},
                    error=reason,
                )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, ctx: MiddlewareContext) -> MiddlewareResult:
        if ctx.llm_response is None:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        output_text = ctx.llm_response.content
        if self._pii_redact and self._pii_guard and output_text:
            redacted = self._pii_guard.redact(output_text)
            if redacted != output_text:
                logger.info("[Guard] 输出 PII 已脱敏")
                ctx.llm_response.content = redacted
                output_text = redacted
        for guard in self._output_guards:
            passed, reason = guard.check(output_text)
            if not passed:
                logger.warning("[Guard] 输出被拦截 [%s]: %s", guard.name, reason)
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
