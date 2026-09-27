"""轻量、安全且不依赖第三方模板引擎的 Prompt 模板。"""

from __future__ import annotations

from dataclasses import dataclass, field
from string import Formatter
from typing import Any, Literal

from agent_core.protocol.messages import Message


class _SafeFormatter(Formatter):
    """只允许简单变量名，禁止属性和下标访问。"""

    def variables(self, template: str) -> tuple[str, ...]:
        values: list[str] = []
        try:
            parts = list(self.parse(template))
        except ValueError as exc:
            raise ValueError(f"Prompt 模板语法错误：{exc}") from exc
        for _literal, name, format_spec, _conversion in parts:
            if name is None:
                continue
            if not name.isidentifier() or "." in name or "[" in name or "]" in name:
                raise ValueError(f"Prompt 模板只允许简单变量名：{name!r}")
            if format_spec and ("{" in format_spec or "}" in format_spec):
                raise ValueError("Prompt 模板的格式说明不允许嵌套变量")
            if name not in values:
                values.append(name)
        return tuple(values)


_FORMATTER = _SafeFormatter()


@dataclass(frozen=True)
class PromptTemplate:
    """单段文本模板，支持默认变量和 partial。"""

    content: str
    defaults: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        variables = set(self.variables)
        unknown_defaults = set(self.defaults) - variables
        if unknown_defaults:
            raise ValueError(f"默认变量未在模板中声明：{sorted(unknown_defaults)}")

    @property
    def variables(self) -> tuple[str, ...]:
        return _FORMATTER.variables(self.content)

    @property
    def required_variables(self) -> tuple[str, ...]:
        return tuple(name for name in self.variables if name not in self.defaults)

    def render(self, **kwargs: Any) -> str:
        values = {**self.defaults, **kwargs}
        missing = [name for name in self.required_variables if name not in values]
        if missing:
            raise ValueError(f"Prompt 模板变量缺失：{', '.join(missing)}")
        try:
            return _FORMATTER.vformat(self.content, (), values)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Prompt 模板渲染失败：{exc}") from exc

    def partial(self, **kwargs: Any) -> PromptTemplate:
        unknown = set(kwargs) - set(self.variables)
        if unknown:
            raise ValueError(f"partial 包含未声明变量：{sorted(unknown)}")
        return PromptTemplate(self.content, defaults={**self.defaults, **kwargs})


@dataclass(frozen=True)
class MessageTemplate:
    """一条带角色的消息模板。"""

    role: Literal["system", "user", "assistant", "tool"]
    template: PromptTemplate | str
    name: str | None = None

    def render(self, **kwargs: Any) -> Message:
        template = (
            self.template if isinstance(self.template, PromptTemplate) else PromptTemplate(self.template)
        )
        return Message(role=self.role, content=template.render(**kwargs), name=self.name)


@dataclass(frozen=True)
class MessagesPlaceholder:
    """在聊天模板中插入一段已经构造好的消息列表。"""

    variable_name: str
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.variable_name.strip():
            raise ValueError("MessagesPlaceholder 变量名不能为空")


@dataclass(frozen=True)
class ChatPromptTemplate:
    """由消息模板和消息占位符组成的聊天 Prompt。"""

    parts: tuple[MessageTemplate | MessagesPlaceholder, ...]
    defaults: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_messages(
        cls,
        parts: list[
            MessageTemplate | MessagesPlaceholder | tuple[Literal["system", "user", "assistant", "tool"], str]
        ],
    ) -> ChatPromptTemplate:
        normalized: list[MessageTemplate | MessagesPlaceholder] = []
        for part in parts:
            if isinstance(part, tuple):
                normalized.append(MessageTemplate(role=part[0], template=part[1]))
            else:
                normalized.append(part)
        return cls(tuple(normalized))

    @property
    def variables(self) -> tuple[str, ...]:
        names: list[str] = []
        for part in self.parts:
            values: tuple[str, ...]
            if isinstance(part, MessagesPlaceholder):
                values = (part.variable_name,)
            else:
                template = (
                    part.template
                    if isinstance(part.template, PromptTemplate)
                    else PromptTemplate(part.template)
                )
                values = template.variables
            for name in values:
                if name not in names:
                    names.append(name)
        return tuple(names)

    def partial(self, **kwargs: Any) -> ChatPromptTemplate:
        unknown = set(kwargs) - set(self.variables)
        if unknown:
            raise ValueError(f"partial 包含未声明变量：{sorted(unknown)}")
        return ChatPromptTemplate(self.parts, defaults={**self.defaults, **kwargs})

    def render(self, **kwargs: Any) -> list[Message]:
        values = {**self.defaults, **kwargs}
        messages: list[Message] = []
        for part in self.parts:
            if isinstance(part, MessagesPlaceholder):
                if part.variable_name not in values:
                    if part.optional:
                        continue
                    raise ValueError(f"消息占位符变量缺失：{part.variable_name}")
                inserted = values[part.variable_name]
                if not isinstance(inserted, list) or not all(isinstance(item, Message) for item in inserted):
                    raise TypeError(f"消息占位符 {part.variable_name} 必须是 list[Message]")
                messages.extend(inserted)
                continue
            messages.append(part.render(**values))
        return messages


__all__ = ["ChatPromptTemplate", "MessageTemplate", "MessagesPlaceholder", "PromptTemplate"]
