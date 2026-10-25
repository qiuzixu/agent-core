"""优雅降级 / Fallback 中间件。

工具调用失败时：
1. 如果有缓存的上次成功结果 → 返回缓存结果（降级但可用）
2. 如果无缓存且工具标记为非关键 → 跳过（STOP + degraded 标记）
3. 如果无缓存且工具是关键的 → 照常 STOP（报错）

after_tool 阶段缓存成功的工具结果（按 tool_name + args 哈希），
before_tool 阶段检查连续失败次数，超过阈值时触发降级逻辑。

配置：
    AGENT_FALLBACK_ENABLED: 是否启用（默认 true）
    AGENT_FALLBACK_FAILURE_THRESHOLD: 连续失败多少次后触发降级（默认 3）
    AGENT_FALLBACK_CRITICAL_TOOLS: 关键工具列表（逗号分隔前缀）
    AGENT_FALLBACK_MAX_CACHE_PER_TOOL: 每个工具最多缓存条目数（默认 50）
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["FallbackConfig", "FallbackMiddleware"]


@dataclass
class FallbackConfig:
    """降级中间件配置。"""

    enabled: bool = True
    failure_threshold: int = 3
    critical_tools: list[str] = field(default_factory=lambda: ["create_", "write_", "delete_", "takeoff", "land"])
    max_cache_per_tool: int = 50
    # 缓存条目有效期（秒），超时后不再作为 fallback
    cache_ttl_seconds: float = 300.0


@dataclass
class _CacheEntry:
    tool_name: str
    args_fingerprint: str
    result: str
    timestamp: float


class FallbackMiddleware(Middleware):
    priority = 40
    """优雅降级：工具连续失败时返回缓存结果或跳过非关键工具。"""

    def __init__(self, config: FallbackConfig | None = None) -> None:
        self._config = config or FallbackConfig()
        self._cache: list[_CacheEntry] = []
        self._failure_counts: dict[str, int] = {}  # {tool_name: consecutive_failures}

    def _args_fingerprint(self, args: dict) -> str:
        return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]

    def _is_critical(self, tool_name: str) -> bool:
        return any(tool_name.startswith(prefix) for prefix in self._config.critical_tools)

    def _find_cached(self, tool_name: str, args_fp: str) -> _CacheEntry | None:
        now = time.monotonic()
        # 清理过期
        self._cache = [e for e in self._cache if (now - e.timestamp) < self._config.cache_ttl_seconds]
        best: _CacheEntry | None = None
        for e in self._cache:
            if e.tool_name == tool_name and e.args_fingerprint == args_fp:
                if best is None or e.timestamp > best.timestamp:
                    best = e
        return best

    def _store_cache(self, tool_name: str, args: dict, result: str) -> None:
        fp = self._args_fingerprint(args)
        # 容量控制
        same_tool = [e for e in self._cache if e.tool_name == tool_name]
        if len(same_tool) >= self._config.max_cache_per_tool:
            oldest = min(same_tool, key=lambda e: e.timestamp)
            self._cache.remove(oldest)
        self._cache.append(_CacheEntry(
            tool_name=tool_name, args_fingerprint=fp, result=result, timestamp=time.monotonic(),
        ))

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled or not context.tool_name:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        tool = context.tool_name
        fp = self._args_fingerprint(context.tool_args)
        fail_count = self._failure_counts.get(tool, 0)

        if fail_count >= self._config.failure_threshold:
            cached = self._find_cached(tool, fp)
            if cached:
                logger.warning("[Fallback] %s 连续失败 %d 次，返回缓存结果", tool, fail_count)
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    data={"fallback_result": cached.result, "degraded": True, "fallback_source": "cache"},
                )
            if not self._is_critical(tool):
                logger.warning("[Fallback] %s 非关键工具，连续失败 %d 次，跳过", tool, fail_count)
                return MiddlewareResult(
                    action=MiddlewareAction.STOP,
                    data={"degraded": True, "fallback_source": "skip", "skip_reason": "tool_degraded"},
                )
            # 关键工具且无缓存 → 照常放行（由熔断器决定是否阻断）
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled or not context.tool_name:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        tool = context.tool_name
        tool_ok = context.tool_result is not None and not context.metadata.get("tool_error")
        if tool_ok:
            self._failure_counts[tool] = 0
            self._store_cache(tool, context.tool_args, context.tool_result or "")
        else:
            self._failure_counts[tool] = self._failure_counts.get(tool, 0) + 1
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)