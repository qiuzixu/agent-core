"""缓存服务工厂。

`create_cache_service()` 是创建缓存服务的统一入口。

支持三种输入：
1. `"memory"`：使用进程内缓存；
2. `"redis"`：使用 Redis 缓存；
3. 传入一个实现了 `CacheBackend` 协议的对象：直接复用自定义后端。
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
    """根据后端类型或自定义 CacheBackend 实例创建统一缓存服务。

    参数说明：
    - backend：内置类型或自定义后端实例；
    - redis_url：backend="redis" 时使用；
    - key_prefix：统一给缓存 key 加前缀；
    - default_ttl_seconds：CacheService 的默认过期时间；
    - max_entries：MemoryCache 的最大容量。
    """
    # 不是字符串说明是业务自定义的 CacheBackend 实例，直接交给 CacheService。
    if not isinstance(backend, str):
        return CacheService(
            backend,
            key_prefix=key_prefix,
            default_ttl_seconds=default_ttl_seconds,
        )

    if backend == "memory":
        # 进程内缓存，适合本地开发和单实例部署。
        return CacheService(
            MemoryCache(max_entries=max_entries),
            key_prefix=key_prefix,
            default_ttl_seconds=default_ttl_seconds,
        )

    if backend == "redis":
        # Redis 后端适合多实例共享缓存。
        return CacheService(
            RedisCache(redis_url=redis_url, key_prefix="agent-core:cache:"),
            key_prefix=key_prefix,
            default_ttl_seconds=default_ttl_seconds,
        )

    # 未知后端直接失败，避免静默回退到错误实现。
    raise ValueError(f"不支持的缓存后端：{backend}")
