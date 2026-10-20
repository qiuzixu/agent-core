"""agent-core 的通用异步缓存模块。

这个包整体解决三件事：
1. 给业务提供一个统一的 `CacheService` 入口；
2. 支持进程内 `MemoryCache` 和可选的分布式 `RedisCache`；
3. 支持 TTL、容量淘汰、key 前缀和并发请求合并。

模块分层：
- `base.py`：后端接口、错误类型和统计模型；
- `memory.py`：进程内默认实现；
- `redis.py`：可选分布式实现；
- `service.py`：业务侧统一门面；
- `factory.py`：根据配置创建缓存服务。
"""

from .base import (
    CacheBackend,
    CacheBackendKind,
    CacheError,
    CacheStats,
)
from .factory import create_cache_service
from .memory import MemoryCache
from .redis import RedisCache
from .service import CacheService

__all__ = [
    "CacheBackend",
    "CacheBackendKind",
    "CacheError",
    "CacheService",
    "CacheStats",
    "MemoryCache",
    "RedisCache",
    "create_cache_service",
]
