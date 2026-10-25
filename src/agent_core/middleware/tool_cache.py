"""工具结果缓存中间件。

依赖 agent_core.cache.base.CacheBackend 协议存储缓存结果。
agent-core 内置进程内存实现；业务层可注入 RedisCache 实现多实例共享。

与 SemanticCacheMiddleware 的区别：
- SemanticCache 缓存 LLM 最终回答（省 LLM token）
- ToolCache 缓存工具执行结果（省 MCP / 业务 API 调用）

配置：
    AGENT_TOOL_CACHE_ENABLED: 是否启用（默认 true）
    AGENT_TOOL_CACHE_DEFAULT_TTL_SECONDS: 默认 TTL（300s = 5min）
    AGENT_TOOL_CACHE_TOOL_TTL: 各工具 TTL 覆盖（JSON 字符串）
    AGENT_TOOL_CACHE_MAX_ENTRIES: 最大缓存条目（默认 200）
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import time
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from agent_core.cache.base import CacheBackend
from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["ToolCacheConfig", "ToolCacheMiddleware", "InMemoryToolCacheStore"]


@dataclass
class ToolCacheConfig:
    """工具缓存配置。"""

    enabled: bool = True
    default_ttl_seconds: float = 300.0
    tool_ttl_map: dict[str, float] = field(default_factory=lambda: {
        "geo_*": 3600.0,
        "*weather*": 600.0,
        "*airspace*": 300.0,
    })
    max_entries: int = 200
    excluded_tools: list[str] = field(default_factory=lambda: [
        "create_", "write_", "delete_", "takeoff", "land",
    ])


class InMemoryToolCacheStore:
    """内置默认工具缓存：进程内 OrderedDict（LRU + TTL）。"""

    def __init__(self, max_entries: int = 200) -> None:
        self._max_entries = max_entries
        self._cache: OrderedDict[str, tuple[float, str, float]] = OrderedDict()

    async def get(self, key: str) -> str | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, value, expiry = entry
        if time.monotonic() > expiry:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return value

    async def set(self, key: str, value: str, *, ttl_seconds: float | None = None) -> None:
        expiry = (time.monotonic() + ttl_seconds) if ttl_seconds is not None else math.inf
        self._cache[key] = (time.monotonic(), value, expiry)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    async def delete(self, key: str) -> bool:
        return self._cache.pop(key, None) is not None

    async def clear(self) -> None:
        self._cache.clear()

    async def close(self) -> None:
        pass


class ToolCacheMiddleware(Middleware):
    priority = 23
    """工具结果缓存：before_tool 查缓存，after_tool 存缓存。

    通过 CacheBackend / ToolCacheStore 协议存取，业务层可注入 RedisCache。
    """

    def __init__(
        self,
        config: ToolCacheConfig | None = None,
        store: CacheBackend | None = None,
    ) -> None:
        self._config = config or ToolCacheConfig()
        self._store = store or InMemoryToolCacheStore(self._config.max_entries)

    def _args_fp(self, args: dict) -> str:
        return hashlib.sha256(
            json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()[:20]

    def _fingerprint(self, tool_name: str, args: dict) -> str:
        return hashlib.sha256(
            f"{tool_name}:{self._args_fp(args)}".encode()
        ).hexdigest()[:24]

    def _is_excluded(self, tool_name: str) -> bool:
        return any(tool_name.startswith(prefix) for prefix in self._config.excluded_tools)

    def _get_ttl(self, tool_name: str) -> float:
        for pattern, ttl in self._config.tool_ttl_map.items():
            if fnmatch.fnmatch(tool_name, pattern):
                return ttl
        return self._config.default_ttl_seconds

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled or not context.tool_name or self._is_excluded(context.tool_name):
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        fp = self._fingerprint(context.tool_name, context.tool_args)
        cached = await self._store.get(fp)
        if cached is not None:
            logger.info("[ToolCache] 命中: %s", context.tool_name)
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                data={"cached_tool_result": cached, "tool_cache_hit": True},
            )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled or not context.tool_name or self._is_excluded(context.tool_name):
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        if context.tool_result is None:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        fp = self._fingerprint(context.tool_name, context.tool_args)
        await self._store.set(fp, str(context.tool_result), ttl_seconds=self._get_ttl(context.tool_name))
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    def get_stats(self) -> dict:
        return {"enabled": self._config.enabled, "store_type": type(self._store).__name__}

