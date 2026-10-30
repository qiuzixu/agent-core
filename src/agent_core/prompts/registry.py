"""与业务无关的提示词版本注册表。

Core 只负责提示词的版本、渲染、回滚和 JSON 文件持久化；具体 Agent 的默认
提示词由应用包注册，避免把某个业务领域的文案带入公共框架。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_core.prompts.templates import PromptTemplate
from agent_core.protocol.messages import Message

logger = logging.getLogger(__name__)


@dataclass
class PromptVersion:
    """单个提示词版本。"""

    version: int
    content: str
    description: str
    defaults: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def variables(self) -> tuple[str, ...]:
        """返回模板声明的变量。"""

        return PromptTemplate(self.content, defaults=self.defaults).variables

    def render(self, **kwargs: Any) -> str:
        """使用 ``str.format`` 渲染模板变量。"""
        return PromptTemplate(self.content, defaults=self.defaults).render(**kwargs)


@dataclass
class PromptEntry:
    """一个提示词的全部版本。"""

    name: str
    current_version: int
    versions: list[PromptVersion] = field(default_factory=list)

    @property
    def current(self) -> PromptVersion:
        for version in self.versions:
            if version.version == self.current_version:
                return version
        raise ValueError(f"找不到版本 {self.current_version}")

    def get_version(self, version: int) -> PromptVersion | None:
        return next((item for item in self.versions if item.version == version), None)


class PromptRegistry:
    """支持版本管理和文件持久化的提示词注册表。"""

    def __init__(self, storage_path: str | Path = "./prompts.json") -> None:
        self._path = Path(storage_path)
        self._entries: dict[str, PromptEntry] = {}
        self._mtimes: dict[str, float] = {}
        self._load()
        self._refresh_mtime_baseline()

    def register(
        self,
        name: str,
        content: str,
        description: str = "",
        *,
        defaults: dict[str, Any] | None = None,
    ) -> PromptVersion:
        """注册提示词；同名提示词会创建新版本。"""
        if name in self._entries:
            return self.update(name, content, description=description, defaults=defaults)
        # 注册时校验模板语法（fail fast），而不是等到渲染时才报错。
        # 校验模板语法（fail fast），变量列表不需要使用。
        _validated = PromptTemplate(content, defaults=dict(defaults or {})).variables
        version = PromptVersion(
            version=1,
            content=content,
            description=description,
            defaults=dict(defaults or {}),
        )
        self._entries[name] = PromptEntry(name=name, current_version=1, versions=[version])
        self._save()
        logger.info("[Prompt] 注册：%s v1", name)
        return version

    def update(
        self,
        name: str,
        content: str,
        description: str = "",
        *,
        defaults: dict[str, Any] | None = None,
    ) -> PromptVersion:
        """创建同一提示词的新版本并切换为当前版本。"""
        entry = self._entries.get(name)
        if entry is None:
            raise KeyError(f"提示词不存在：{name}，请先调用 register()")
        # 更新时同样校验模板语法。
        # 校验模板语法（fail fast），变量列表不需要使用。
        _validated = PromptTemplate(content, defaults=dict(defaults or {})).variables
        version = PromptVersion(
            version=max(item.version for item in entry.versions) + 1,
            content=content,
            description=description,
            defaults=dict(defaults or {}),
        )
        entry.versions.append(version)
        entry.current_version = version.version
        self._save()
        logger.info("[Prompt] 更新：%s v%d", name, version.version)
        return version

    def rollback(self, name: str, version: int) -> PromptVersion:
        """切换到已有版本，不删除版本历史。"""
        entry = self._get_entry(name)
        target = entry.get_version(version)
        if target is None:
            raise KeyError(f"提示词 {name} 不存在版本 {version}")
        entry.current_version = version
        self._save()
        logger.info("[Prompt] 回滚：%s -> v%d", name, version)
        return target

    def render(self, prompt_name: str, **kwargs: Any) -> str:
        """渲染当前版本。"""
        return self._get_entry(prompt_name).current.render(**kwargs)

    def render_messages(
        self,
        prompt_name: str,
        *,
        system_variables: dict[str, Any] | None = None,
        user_variables: dict[str, Any] | None = None,
        history: list[Message] | None = None,
    ) -> list[Message]:
        """把当前版本渲染为消息列表（system + history + user）。

        这是 ChatPromptTemplate 的运行时入口：调用方只需要提供变量和历史
        消息，不需要手动构造 Message 对象。
        """
        content = self._get_entry(prompt_name).current.render(**(system_variables or {}))
        messages: list[Message] = [Message(role="system", content=content)]
        if history:
            messages.extend(history)
        if user_variables:
            user_content = ", ".join(
                f"{key}={value}" for key, value in user_variables.items()
            )
            messages.append(Message(role="user", content=user_content))
        return messages

    def get(self, prompt_name: str) -> str:
        """读取当前版本的原始模板。"""
        return self._get_entry(prompt_name).current.content

    def get_history(self, prompt_name: str) -> list[dict[str, Any]]:
        """返回版本历史摘要。"""
        entry = self._get_entry(prompt_name)
        return [
            {
                "version": item.version,
                "description": item.description,
                "created_at": item.created_at,
                "variables": list(item.variables),
                "is_current": item.version == entry.current_version,
                "content_preview": item.content[:100] + "..." if len(item.content) > 100 else item.content,
            }
            for item in entry.versions
        ]

    def list_prompts(self) -> list[str]:
        """列出已注册的提示词名称。"""
        return list(self._entries)

    def delete(self, name: str) -> bool:
        """删除提示词及其历史。"""
        if name not in self._entries:
            return False
        del self._entries[name]
        self._save()
        logger.info("[Prompt] 删除：%s", name)
        return True

    def _save(self) -> None:
        data = {
            name: {
                "name": entry.name,
                "current_version": entry.current_version,
                "versions": [
                    {
                        "version": item.version,
                        "content": item.content,
                        "description": item.description,
                        "defaults": item.defaults,
                        "created_at": item.created_at,
                    }
                    for item in entry.versions
                ],
            }
            for name, entry in self._entries.items()
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(data, temporary, ensure_ascii=False, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self._path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)
        self._refresh_mtime_baseline()

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"提示词注册表根节点必须是对象：{self._path}")
        loaded: dict[str, PromptEntry] = {}
        for name, raw in data.items():
            versions = [
                PromptVersion(
                    version=item["version"],
                    content=item["content"],
                    description=item.get("description", ""),
                    defaults=dict(item.get("defaults") or {}),
                    created_at=item.get("created_at", ""),
                )
                for item in raw["versions"]
            ]
            loaded[name] = PromptEntry(
                name=raw.get("name", name),
                current_version=raw["current_version"],
                versions=versions,
            )
        # 全量解析成功后再替换内存状态，损坏文件不会变成“空注册表”。
        self._entries = loaded
        logger.info("[Prompt] 从文件加载 %d 个提示词：%s", len(self._entries), self._path)

    def _get_entry(self, name: str) -> PromptEntry:
        entry = self._entries.get(name)
        if entry is None:
            raise KeyError(f"提示词不存在：{name}")
        return entry

    def _refresh_mtime_baseline(self) -> None:
        """记录当前文件的 mtime，作为热重载的变更检测基线。"""
        try:
            stat = self._path.stat()
            self._mtimes[str(self._path)] = stat.st_mtime
        except OSError:
            self._mtimes.pop(str(self._path), None)

    def check_for_changes(self) -> bool:
        """检测存储文件是否被外部修改（mtime 变化）。

        返回 True 表示文件有变化，需要调用 reload_changed() 重新加载。
        """
        try:
            stat = self._path.stat()
        except OSError:
            return str(self._path) in self._mtimes
        baseline = self._mtimes.get(str(self._path))
        if baseline is None:
            return True
        return stat.st_mtime != baseline

    def reload_changed(self) -> int:
        """检测文件变化并重新加载；返回新加载的提示词数量。

        与 skills registry 一致：先加载，成功后才替换内存状态。
        文件损坏时保留当前内存数据并输出 WARNING。
        """
        if not self.check_for_changes():
            return 0
        try:
            old_count = len(self._entries)
            self._load()
            self._refresh_mtime_baseline()
            new_count = len(self._entries)
            logger.info("[Prompt] 热重载成功：%d -> %d 个提示词", old_count, new_count)
            return new_count
        except Exception as exc:
            logger.warning("[Prompt] 热重载失败，保留当前内存数据（原因：%s）", exc)
            return 0


__all__ = ["PromptEntry", "PromptRegistry", "PromptVersion"]
