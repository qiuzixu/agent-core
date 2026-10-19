from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from .base import CacheBackend, CacheStats
from .memory import MemoryCache

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
        self._backend = backend or MemoryCache()
        self._key_prefix = key_prefix
        self._default_ttl_seconds = default_ttl_seconds
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self._inflight_lock = asyncio.Lock()
        self.stats = CacheStats()

    def key(self, key: str) -> str:
        return f"{self._key_prefix}{key}" if self._key_prefix else key

    async def get(self, key: str) -> Any | None:
        value = await self._backend.get(self.key(key))
        if value is None:
            await self.stats.record_miss()
            return None
        await self.stats.record_hit()
        return value

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None = None) -> None:
        effective_ttl = self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        await self._backend.set(self.key(key), value, ttl_seconds=effective_ttl)

    async def delete(self, key: str) -> bool:
        return await self._backend.delete(self.key(key))

    async def clear(self) -> None:
        await self._backend.clear()

    async def get_or_load(
        self,
        key: str,
        loader: Callable[[], Awaitable[T] | T],
        *,
        ttl_seconds: float | None = None,
    ) -> T:
        cached = await self.get(key)
        if cached is not None:
            return cast("T", cached)

        async with self._inflight_lock:
            existing = self._inflight.get(self.key(key))
            if existing is not None:
                await self.stats.record_load(shared=True)
                return cast("T", await existing)

            async def run_loader() -> T:
                try:
                    value = loader()
                    if inspect.isawaitable(value):
                        value = await value
                    if value is not None:
                        await self.set(key, value, ttl_seconds=ttl_seconds)
                    return value
                finally:
                    async with self._inflight_lock:
                        self._inflight.pop(self.key(key), None)

            task = asyncio.create_task(run_loader(), name=f"cache-load:{self.key(key)}")
            self._inflight[self.key(key)] = task

        await self.stats.record_load()
        return await task

    async def close(self) -> None:
        for task in list(self._inflight.values()):
            if not task.done():
                task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)
        self._inflight.clear()
        await self._backend.close()
