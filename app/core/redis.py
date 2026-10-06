from fastapi import Request
from redis.asyncio import Redis


def create_redis(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True, health_check_interval=30)


async def get_redis_cache(request: Request) -> Redis:
    redis: Redis = request.app.state.redis_cache
    return redis
