from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .base import CacheError


class RedisCache:
    """可选 Redis 后端；不强制 Core 依赖 redis 包。"""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        client: Any | None = None,
        key_prefix: str = "agent-core:cache:",
        serializer: Callable[[Any], str] | None = None,
        deserializer: Callable[[str], Any] | None = None,
    ) -> None:
        if client is None:
            try:
                from redis.asyncio import Redis
            except ImportError as exc:
                raise CacheError("使用 RedisCache 需要先安装 redis 依赖") from exc
            if not redis_url:
                raise ValueError("RedisCache 需要 redis_url")
            self._client: Any = Redis.from_url(redis_url, decode_responses=True)
        else:
            self._client = client
        self._key_prefix = key_prefix
        self._serializer = serializer or self._default_serialize
        self._deserializer = deserializer or self._default_deserialize

    def _key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    @staticmethod
    def _default_serialize(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _default_deserialize(raw: str) -> Any:
        return json.loads(raw)

    async def get(self, key: str) -> Any | None:
        raw = await self._client.get(self._key(key))
        return None if raw is None else self._deserializer(raw)

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None) -> None:
        payload = self._serializer(value)
        if ttl_seconds and ttl_seconds > 0:
            await self._client.set(self._key(key), payload, px=max(1, int(ttl_seconds * 1000)))
        else:
            await self._client.set(self._key(key), payload)

    async def delete(self, key: str) -> bool:
        return bool(await self._client.delete(self._key(key)))

    async def clear(self) -> None:
        cursor = 0
        while True:
            cursor, keys = await self._client.scan(cursor, match=f"{self._key_prefix}*")
            if keys:
                await self._client.delete(*keys)
            if cursor == 0:
                break

    async def close(self) -> None:
        await self._client.aclose()
