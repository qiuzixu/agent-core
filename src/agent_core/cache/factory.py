"""缓存服务工厂。

支持两种方式创建：
1. 传入后端类型字符串（"memory" / "redis"）— 使用内置默认实现
2. 传入 CacheBackend 实例 — 业务层自定义适配器（与 ports 模式一致）
"""

from __future__ import annotations

from .base import CacheBackend, CacheBackendKind
from .memory import MemoryCache
from .redis import RedisCache
from .service import CacheService


def create_cache_service(
    backend: CacheBackendKind | CacheBackend = "memory",
    *,
    redis_url: str | None = None,
    key_prefix: str = "",
    default_ttl_seconds: float | None = 60.0,
    max_entries: int = 1024,
) -> CacheService:
    """根据后端类型或自定义 CacheBackend 实例创建统一缓存服务。"""
    # 业务层自定义适配器（实现 CacheBackend 协议即可）
    if hasattr(backend, "get") and hasattr(backend, "set") and hasattr(backend, "delete"):
        return CacheService(
            backend,
            key_prefix=key_prefix,
            default_ttl_seconds=default_ttl_seconds,
        )
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
