from __future__ import annotations

import asyncio
import unittest

from agent_core.cache import CacheService, MemoryCache


class CacheServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_cache_hit_miss_and_ttl(self) -> None:
        service = CacheService(MemoryCache(max_entries=8), key_prefix="test:", default_ttl_seconds=0.01)
        await service.set("weather", {"city": "深圳"})
        self.assertEqual(await service.get("weather"), {"city": "深圳"})
        await asyncio.sleep(0.02)
        self.assertIsNone(await service.get("weather"))

    async def test_get_or_load_merges_concurrent_calls(self) -> None:
        service = CacheService(MemoryCache(max_entries=8), default_ttl_seconds=60)
        calls = 0
        gate = asyncio.Event()

        async def slow_loader() -> str:
            nonlocal calls
            calls += 1
            await gate.wait()
            return "loaded"

        first = asyncio.create_task(service.get_or_load("key", slow_loader))
        await asyncio.sleep(0)
        second = asyncio.create_task(service.get_or_load("key", slow_loader))
        gate.set()
        self.assertEqual(await asyncio.gather(first, second), ["loaded", "loaded"])
        self.assertEqual(calls, 1)
        self.assertEqual(await service.get("key"), "loaded")

    async def test_lru_eviction_keeps_recent_keys(self) -> None:
        cache = MemoryCache(max_entries=2)
        await cache.set("a", 1, ttl_seconds=None)
        await cache.set("b", 2, ttl_seconds=None)
        self.assertEqual(await cache.get("a"), 1)
        await cache.set("c", 3, ttl_seconds=None)
        self.assertIsNone(await cache.get("b"))


if __name__ == "__main__":
    unittest.main()
