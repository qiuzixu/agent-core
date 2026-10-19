from __future__ import annotations

from .base import CacheBackendKind
from .memory import MemoryCache
from .redis import RedisCache
from .service import CacheService


def create_cache_service(
    backend: CacheBackendKind = "memory",
    *,
    redis_url: str | None = None,
    key_prefix: str = "",
    default_ttl_seconds: float | None = 60.0,
    max_entries: int = 1024,
) -> CacheService:
    """根据后端类型创建统一缓存服务；默认只依赖进程内存。"""
    if backend == "memory":
        return CacheService(
            MemoryCache(max_entries=max_entries),
            key_prefix=key_prefix,
            default_ttl_seconds=default_ttl_seconds,
        )
    if backend == "redis":
        return CacheService(
            RedisCache(redis_url=redis_url, key_prefix="agent-core:cache:"),
            key_prefix=key_prefix,
            default_ttl_seconds=default_ttl_seconds,
        )
    raise ValueError(f"不支持的缓存后端：{backend}")
