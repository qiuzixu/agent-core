"""Secrets 管理：统一密钥读取入口。

Ports & Adapters：本模块定义 SecretsProvider 协议和两个内置适配器
（环境变量 / JSON 文件）。业务层可注入 Vault / KMS 适配器。

用法::

    provider = ChainedSecretsProvider([EnvSecretsProvider("SECRET_"), ...])
    value = resolve_secret("${SECRET:OPENAI_API_KEY}", provider)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Protocol

logger = logging.getLogger(__name__)

_SECRET_PATTERN = re.compile(r"\$\{SECRET:([A-Za-z0-9_.-]+)\}")


class SecretsProvider(Protocol):
    """密钥提供者协议。"""

    def get(self, key: str) -> str | None: ...


class EnvSecretsProvider:
    """从环境变量读取密钥（默认前缀 SECRET_）。"""

    def __init__(self, prefix: str = "SECRET_") -> None:
        self._prefix = prefix.upper()

    def get(self, key: str) -> str | None:
        return os.environ.get(f"{self._prefix}{key.upper()}")


class FileSecretsProvider:
    """从 JSON 文件读取密钥。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._cache: dict[str, str] | None = None

    def get(self, key: str) -> str | None:
        if self._cache is None:
            try:
                raw = json.loads(open(self._path, encoding="utf-8").read())
                self._cache = raw if isinstance(raw, dict) else {}
            except (OSError, ValueError):
                self._cache = {}
        return self._cache.get(key)


class ChainedSecretsProvider:
    """按顺序尝试多个提供者。"""

    def __init__(self, providers: list) -> None:
        self._providers = providers

    def get(self, key: str) -> str | None:
        for provider in self._providers:
            try:
                value = provider.get(key)
            except Exception:
                logger.exception("SecretsProvider %r 读取 %s 失败", provider, key)
                continue
            if value is not None:
                return value
        return None


def resolve_secret(value: str, provider: SecretsProvider) -> str:
    """把 ${SECRET:KEY} 占位符替换为真实值。"""

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        resolved = provider.get(key)
        if resolved is None:
            logger.warning("密钥 %s 未找到，保留占位符", key)
            return match.group(0)
        return resolved

    return _SECRET_PATTERN.sub(_sub, value)


def resolve_config(config, provider):
    """递归解析 dict/list/str 中的密钥占位符。"""
    if isinstance(config, dict):
        return {k: resolve_config(v, provider) for k, v in config.items()}
    if isinstance(config, list):
        return [resolve_config(item, provider) for item in config]
    if isinstance(config, str):
        return resolve_secret(config, provider)
    return config


__all__ = [
    "ChainedSecretsProvider", "EnvSecretsProvider", "FileSecretsProvider",
    "SecretsProvider", "resolve_config", "resolve_secret",
]
