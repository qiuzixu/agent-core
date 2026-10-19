"""通用缓存服务：TTL/LRU、请求合并和可替换后端。"""

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
