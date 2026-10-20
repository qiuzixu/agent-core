"""进程内缓存实现。

`MemoryCache` 是最简单的默认后端：
- 数据保存在当前 Python 进程里；
- 用 `OrderedDict` 实现 LRU 淘汰；
- 用单调时间实现 TTL；
- 适合单实例开发、测试或轻量部署。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class MemoryCache:
    """进程内 TTL + LRU 缓存；适合单实例开发和轻量部署。"""

    def __init__(self, *, max_entries: int = 1024) -> None:
        # 最多保留多少个 key，超过后会淘汰最久未使用的条目。
        if max_entries <= 0:
            raise ValueError("max_entries 必须大于 0")

        # value 后面的第二个元素是过期时间点；None 表示不过期。
        self._entries: OrderedDict[str, tuple[Any, float | None]] = OrderedDict()
        self._max_entries = max_entries

    async def get(self, key: str) -> Any | None:
        # 1. key 不存在，返回 None；
        # 2. key 存在但已过期，删除并返回 None；
        # 3. key 可用，移动到末尾表示最近使用。
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
        # 计算绝对过期时间点；没有 TTL 时不过期。
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds and ttl_seconds > 0 else None

        # 先删旧 key，是为了保证写操作后这个 key 一定位于最新位置。
        if key in self._entries:
            del self._entries[key]

        # 超过容量时淘汰最久未使用的条目。
        while len(self._entries) >= self._max_entries:
            self._entries.popitem(last=False)

        self._entries[key] = (value, expires_at)

    async def delete(self, key: str) -> bool:
        # 返回是否真的删除了数据。
        return self._entries.pop(key, None) is not None

    async def clear(self) -> None:
        # 清空整个进程内缓存。
        self._entries.clear()

    async def close(self) -> None:
        # 进程内缓存没有连接资源需要释放。
        return None
