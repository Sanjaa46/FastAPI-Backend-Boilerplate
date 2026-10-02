import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger()
ModelT =TypeVar("ModelT", bound=BaseModel)


class CacheService:
    def __init__(self, redis: Redis, *, prefix: str, default_ttl: int) -> None:
        self._redis = redis
        self._prefix = prefix
        self._default_ttl = default_ttl

    def key(self, *parts: str | int) -> str:
        """Build a namespaced key, e.g. app:users:v1:<id>. Bump `v1` when the schema changes."""
        return ":".join((self._prefix, *(str(p) for p in parts)))

    async def get(self, key: str, model: type[ModelT]) -> ModelT | None:
        try:
            raw = await self._redis.get(key)
        except RedisError:
            log.warning("cache_get_failed", key=key, exc_info=True)
            return None
        return None if raw is None else model.model_validate_json(raw)

    async def set(self, key: str, value: BaseModel, ttl: int | None = None) -> None:
        base_ttl = ttl or self._default_ttl
        # Jitter (+0-10%) spreads expirations so hot keys don't all expire together.
        expire = base_ttl + random.randint(0, max(1, base_ttl // 10))   # noqa: S311
        try:
            await self._redis.set(key, value.model_dump_json(), ex=expire)
        except RedisError:
            log.warning("cache_set_failed", key=key, exc_info=True)

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self._redis.delete(*keys)
        except RedisError:
            log.warning("cache_delete_failed", keys=keys, exc_info=True)

    async def get_or_load(
        self,
        key: str,
        model: type[ModelT],
        loader: Callable[[], Awaitable[ModelT]],
        ttl: int | None = None,
    ) -> ModelT:
        """Return the cached value, or call `loader`, cache the result, and return it."""
        cached = await self.get(key, model)
        if cached is not None:
            return cached
        value = await loader()
        await self.set(key, value, ttl)
        return value