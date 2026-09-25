"""与业务无关的提示词版本注册表。

Core 只负责提示词的版本、渲染、回滚和 JSON 文件持久化；具体 Agent 的默认
提示词由应用包注册，避免把某个业务领域的文案带入公共框架。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PromptVersion:
    """单个提示词版本。"""

    version: int
    content: str
    description: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def render(self, **kwargs: Any) -> str:
        """使用 ``str.format`` 渲染模板变量。"""
        try:
            return self.content.format(**kwargs)
        except KeyError as exc:
            raise ValueError(
                f"提示词变量缺失：{exc}，可用变量：{list(kwargs.keys())}"
            ) from exc


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
        self._load()

    def register(self, name: str, content: str, description: str = "") -> PromptVersion:
        """注册提示词；同名提示词会创建新版本。"""
        if name in self._entries:
            return self.update(name, content, description=description)
        version = PromptVersion(version=1, content=content, description=description)
        self._entries[name] = PromptEntry(name=name, current_version=1, versions=[version])
        self._save()
        logger.info("[Prompt] 注册：%s v1", name)
        return version

    def update(self, name: str, content: str, description: str = "") -> PromptVersion:
        """创建同一提示词的新版本并切换为当前版本。"""
        entry = self._entries.get(name)
        if entry is None:
            raise KeyError(f"提示词不存在：{name}，请先调用 register()")
        version = PromptVersion(
            version=max(item.version for item in entry.versions) + 1,
            content=content,
            description=description,
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
                "is_current": item.version == entry.current_version,
                "content_preview": item.content[:100] + "..."
                if len(item.content) > 100
                else item.content,
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
                        "created_at": item.created_at,
                    }
                    for item in entry.versions
                ],
            }
            for name, entry in self._entries.items()
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for name, raw in data.items():
                versions = [
                    PromptVersion(
                        version=item["version"],
                        content=item["content"],
                        description=item.get("description", ""),
                        created_at=item.get("created_at", ""),
                    )
                    for item in raw["versions"]
                ]
                self._entries[name] = PromptEntry(
                    name=raw.get("name", name),
                    current_version=raw["current_version"],
                    versions=versions,
                )
            logger.info("[Prompt] 从文件加载 %d 个提示词：%s", len(self._entries), self._path)
        except Exception as exc:
            logger.error("[Prompt] 加载失败：%s", exc)

    def _get_entry(self, name: str) -> PromptEntry:
        entry = self._entries.get(name)
        if entry is None:
            raise KeyError(f"提示词不存在：{name}")
        return entry


__all__ = ["PromptEntry", "PromptRegistry", "PromptVersion"]
