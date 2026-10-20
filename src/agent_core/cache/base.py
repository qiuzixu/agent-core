"""缓存模块的基础契约层。

这里不保存真实缓存数据，只定义：
- 允许的后端类型；
- 后端必须实现的异步协议；
- 缓存模块统一错误；
- 命中率、加载次数等统计信息。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# 工厂函数内置支持的后端类型；自定义后端可以直接实现 CacheBackend。
CacheBackendKind = Literal["memory", "redis"]


class CacheError(RuntimeError):
    """缓存层统一错误，避免业务直接依赖底层 Redis 或驱动异常。"""


class CacheBackend(Protocol):
    """缓存后端的异步接口协议。

    MemoryCache、RedisCache 或业务自定义适配器都需要实现这些方法。
    """

    async def get(self, key: str) -> Any | None:
        """读取缓存；返回 None 表示未命中。"""
        ...

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None) -> None:
        """写入缓存；ttl_seconds=None 表示不设置主动过期时间。"""
        ...

    async def delete(self, key: str) -> bool:
        """删除指定 key，返回是否真的删除了数据。"""
        ...

    async def clear(self) -> None:
        """清空当前缓存命名空间下的数据。"""
        ...

    async def close(self) -> None:
        """释放底层连接或资源；进程内实现通常可以留空。"""
        ...


@dataclass(slots=True)
class CacheStats:
    """缓存运行统计，用于观察命中率和请求合并效果。"""

    # 命中次数。
    hits: int = 0
    # 未命中次数。
    misses: int = 0
    # 真实执行 loader 的次数。
    loads: int = 0
    # 命中已有加载任务、没有重复回源的次数。
    shared_loads: int = 0
    # 因容量限制或过期被淘汰的条目数。
    evictions: int = 0
    # 后端调用失败次数。
    errors: int = 0
    # 统计更新锁，保证同一事件循环内的并发安全。
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def record_hit(self) -> None:
        """记录一次命中。"""
        async with self._lock:
            self.hits += 1

    async def record_miss(self) -> None:
        """记录一次未命中。"""
        async with self._lock:
            self.misses += 1

    async def record_load(self, *, shared: bool = False) -> None:
        """记录一次数据加载；shared=True 表示复用了进行中的加载任务。"""
        async with self._lock:
            self.loads += 1
            if shared:
                self.shared_loads += 1

    async def record_eviction(self) -> None:
        """记录一次淘汰。"""
        async with self._lock:
            self.evictions += 1

    def snapshot(self) -> dict[str, int]:
        """导出当前统计快照。"""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "loads": self.loads,
            "shared_loads": self.shared_loads,
            "evictions": self.evictions,
            "errors": self.errors,
        }
