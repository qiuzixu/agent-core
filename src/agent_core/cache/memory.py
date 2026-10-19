from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class MemoryCache:
    """进程内 TTL + LRU 缓存；适合单实例开发和轻量部署。"""

    def __init__(self, *, max_entries: int = 1024) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries 必须大于 0")
        self._entries: OrderedDict[str, tuple[Any, float | None]] = OrderedDict()
        self._max_entries = max_entries

    async def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and expires_at <= time.monotonic():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None) -> None:
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds and ttl_seconds > 0 else None
        if key in self._entries:
            del self._entries[key]
        while len(self._entries) >= self._max_entries:
            self._entries.popitem(last=False)
        self._entries[key] = (value, expires_at)

    async def delete(self, key: str) -> bool:
        return self._entries.pop(key, None) is not None

    async def clear(self) -> None:
        self._entries.clear()

    async def close(self) -> None:
        return None
