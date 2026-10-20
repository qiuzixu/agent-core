"""缓存服务的业务门面层。

业务代码通常直接使用 `CacheService`，而不是直接使用 `MemoryCache` 或 `RedisCache`。

它负责：
- 统一 get / set / delete / clear API；
- 统一处理默认 TTL 和 key 前缀；
- 统计命中和未命中；
- 对回源函数做请求合并，避免并发重复加载。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from .base import CacheBackend, CacheStats
from .memory import MemoryCache

# loader 返回的数据类型。
T = TypeVar("T")


class CacheService:
    """进程内请求合并 + 可替换缓存后端。

    单实例时默认使用 MemoryCache；多实例部署时可切换 RedisCache。
    """

    def __init__(
        self,
        backend: CacheBackend | None = None,
        *,
        key_prefix: str = "",
        default_ttl_seconds: float | None = 60.0,
    ) -> None:
        # 没有显式传入后端时，默认使用进程内缓存。
        self._backend = backend or MemoryCache()
        self._key_prefix = key_prefix
        self._default_ttl_seconds = default_ttl_seconds

        # 正在执行中的回源任务；用于把多个并发请求合并到同一个 loader。
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._inflight_lock = asyncio.Lock()
        self.stats = CacheStats()

    def key(self, key: str) -> str:
        # 对外仍使用业务 key；落到后端时统一加前缀。
        return f"{self._key_prefix}{key}" if self._key_prefix else key

    async def get(self, key: str) -> Any | None:
        value = await self._backend.get(self.key(key))

        # None 表示未命中，缓存命中则记录命中统计。
        if value is None:
            await self.stats.record_miss()
            return None

        await self.stats.record_hit()
        return value

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> None:
        # 调用方没传 TTL 时，使用服务默认 TTL。
        effective_ttl = self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        await self._backend.set(self.key(key), value, ttl_seconds=effective_ttl)

    async def delete(self, key: str) -> bool:
        # 删除指定业务 key。
        return await self._backend.delete(self.key(key))

    async def clear(self) -> None:
        # 清空当前后端中该缓存命名空间的数据。
        await self._backend.clear()

    async def get_or_load(
        self,
        key: str,
        loader: Callable[[], Awaitable[T] | T],
        *,
        ttl_seconds: float | None = None,
    ) -> T:
        """读取缓存；未命中时执行 loader 并写回缓存。

        这是本模块最常用的方法之一：
        1. 先查缓存；
        2. 缓存有值直接返回；
        3. 缓存没有值时检查是否已有相同 key 的回源任务；
        4. 如果有，就等待同一个任务，避免并发重复回源；
        5. 如果没有，创建新任务，加载成功后写回缓存。
        """
        cached = await self.get(key)
        if cached is not None:
            return cast("T", cached)

        # 短暂持锁检查并登记加载任务；真正的 loader 执行不放在锁内。
        async with self._inflight_lock:
            existing = self._inflight.get(self.key(key))
            if existing is not None:
                # 已经有另一个请求在加载相同 key，这里复用它。
                await self.stats.record_load(shared=True)
                return cast("T", await existing)

            async def run_loader() -> T:
                try:
                    value = loader()
                    if inspect.isawaitable(value):
                        value = await value

                    # loader 返回 None 时只返回，不写缓存，避免把“无数据”当成有效缓存。
                    if value is not None:
                        await self.set(key, value, ttl_seconds=ttl_seconds)
                    return value
                finally:
                    # 无论成功、失败还是取消，都要清理进行中的任务。
                    async with self._inflight_lock:
                        self._inflight.pop(self.key(key), None)

            task = asyncio.create_task(run_loader(), name=f"cache-load:{self.key(key)}")
            self._inflight[self.key(key)] = task

        await self.stats.record_load()
        return await task

    async def close(self) -> None:
        """取消仍在执行的回源任务并关闭后端。"""
        for task in list(self._inflight.values()):
            if not task.done():
                task.cancel()

        # 等待任务真正退出，忽略取消或加载异常。
        if self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)

        self._inflight.clear()
        await self._backend.close()
