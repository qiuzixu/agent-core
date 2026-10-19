from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

CacheBackendKind = Literal["memory", "redis"]


class CacheError(RuntimeError):
    """缓存层统一错误。"""


class CacheBackend(Protocol):
    """缓存后端协议；Core 不绑定具体存储实现。"""

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None) -> None: ...

    async def delete(self, key: str) -> bool: ...

    async def clear(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class CacheStats:
    """缓存调用统计，用于观测命中率和请求合并效果。"""

    hits: int = 0
    misses: int = 0
    loads: int = 0
    shared_loads: int = 0
    evictions: int = 0
    errors: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def record_hit(self) -> None:
        async with self._lock:
            self.hits += 1

    async def record_miss(self) -> None:
        async with self._lock:
            self.misses += 1

    async def record_load(self, *, shared: bool = False) -> None:
        async with self._lock:
            self.loads += 1
            if shared:
                self.shared_loads += 1

    async def record_eviction(self) -> None:
        async with self._lock:
            self.evictions += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "loads": self.loads,
            "shared_loads": self.shared_loads,
            "evictions": self.evictions,
            "errors": self.errors,
        }
