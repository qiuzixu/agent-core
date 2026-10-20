"""可选 Redis 缓存实现。

`RedisCache` 适合多实例部署：
- 多个 Agent 实例可以共享同一份缓存；
- Redis key 会统一加前缀，避免污染其他业务；
- 默认用 JSON 序列化；
- Redis 客户端可以注入，方便测试或复用连接。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .base import CacheError


class RedisCache:
    """可选 Redis 后端；不强制 Core 直接依赖 redis 包。"""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        client: Any | None = None,
        key_prefix: str = "agent-core:cache:",
        serializer: Callable[[Any], str] | None = None,
        deserializer: Callable[[str], Any] | None = None,
    ) -> None:
        # 未注入客户端时才尝试创建 Redis 连接。
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
        # 统一加上业务前缀，避免多个服务共用同一个 Redis 时 key 冲突。
        return f"{self._key_prefix}{key}"

    @staticmethod
    def _default_serialize(value: Any) -> str:
        # 默认序列化为 UTF-8 JSON 字符串。
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _default_deserialize(raw: str) -> Any:
        # 读取时再反序列化回 Python 对象。
        return json.loads(raw)

    async def get(self, key: str) -> Any | None:
        raw = await self._client.get(self._key(key))
        # Redis 没有命中时返回 None；命中时反序列化后返回。
        return None if raw is None else self._deserializer(raw)

    async def set(self, key: str, value: Any, *, ttl_seconds: float | None) -> None:
        payload = self._serializer(value)

        # TTL 大于 0 时使用 PX 毫秒过期；否则不过期。
        if ttl_seconds and ttl_seconds > 0:
            await self._client.set(self._key(key), payload, px=max(1, int(ttl_seconds * 1000)))
        else:
            await self._client.set(self._key(key), payload)

    async def delete(self, key: str) -> bool:
        # Redis DELETE 返回被删除 key 的数量，这里转成 bool。
        return bool(await self._client.delete(self._key(key)))

    async def clear(self) -> None:
        # 用 SCAN 按前缀扫描并删除，避免一次性 KEYS 命令阻塞 Redis。
        cursor = 0
        while True:
            cursor, keys = await self._client.scan(cursor, match=f"{self._key_prefix}*")
            if keys:
                await self._client.delete(*keys)
            if cursor == 0:
                break

    async def close(self) -> None:
        # 关闭 Redis 异步连接。
        await self._client.aclose()
