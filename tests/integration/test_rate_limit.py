"""The sliding-window limiter against a real Redis, with a fake clock (no sleeping)."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Rate
from app.core.rate_limit import RateLimiter, RateLimitResult
from app.core.redis import create_redis

# Aligned to a window start (60 s), so offsets below are offsets into a window.
T0 = 60.0 * 10_000


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def limiter(redis_cache: Redis, clock: Clock) -> RateLimiter:
    return RateLimiter(redis_cache, prefix="test:ratelimit", clock=clock)


async def hit(
    limiter: RateLimiter, rate: Rate, scope: str = "s", identifier: str = "c"
) -> RateLimitResult:
    result = await limiter.hit(scope, identifier, rate)
    assert result is not None
    return result


async def test_allows_up_to_the_limit_then_rejects(limiter: RateLimiter) -> None:
    results = [await hit(limiter, Rate(3, 60)) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert results[3].limit == 3
    assert results[3].retry_after > 0


async def test_rejected_requests_are_not_counted(limiter: RateLimiter, clock: Clock) -> None:
    rate = Rate(2, 10)
    await hit(limiter, rate)
    await hit(limiter, rate)
    for _ in range(50):  # hammering must not extend the penalty
        denied = await hit(limiter, rate)
    assert not denied.allowed

    clock.now += denied.retry_after
    assert (await hit(limiter, rate)).allowed


async def test_no_double_burst_across_a_window_boundary(limiter: RateLimiter, clock: Clock) -> None:
    """A fixed window would admit 4 at +59 s AND 4 at +60 s. The sliding window must not."""
    rate = Rate(4, 60)
    clock.now = T0 + 59
    assert all([(await hit(limiter, rate)).allowed for _ in range(4)])

    clock.now = T0 + 60  # a new fixed window, but the last one still weighs fully
    denied = await hit(limiter, rate)
    assert not denied.allowed
    assert denied.retry_after == 15  # exactly when one request's worth has decayed

    clock.now = T0 + 60 + 14
    assert not (await hit(limiter, rate)).allowed
    clock.now = T0 + 60 + 15
    assert (await hit(limiter, rate)).allowed


async def test_retry_after_when_the_current_window_is_full(
    limiter: RateLimiter, clock: Clock
) -> None:
    rate = Rate(2, 60)
    clock.now = T0 + 10
    await hit(limiter, rate)
    await hit(limiter, rate)

    clock.now = T0 + 20
    denied = await hit(limiter, rate)
    assert not denied.allowed
    assert denied.retry_after == 70  # 40 s to the next window + window/limit (30 s) to decay

    clock.now = T0 + 20 + 69
    assert not (await hit(limiter, rate)).allowed
    clock.now = T0 + 20 + 70
    assert (await hit(limiter, rate)).allowed


async def test_full_allowance_returns_after_two_windows(limiter: RateLimiter, clock: Clock) -> None:
    rate = Rate(3, 60)
    for _ in range(3):
        await hit(limiter, rate)
    clock.now = T0 + 120
    result = await hit(limiter, rate)
    assert result.allowed
    assert result.remaining == 2


async def test_identifiers_and_scopes_are_independent(limiter: RateLimiter) -> None:
    rate = Rate(1, 60)
    assert (await hit(limiter, rate, "login", "a")).allowed
    assert not (await hit(limiter, rate, "login", "a")).allowed
    assert (await hit(limiter, rate, "login", "b")).allowed
    assert (await hit(limiter, rate, "register", "a")).allowed


async def test_counters_expire(limiter: RateLimiter, redis_cache: Redis) -> None:
    await hit(limiter, Rate(5, 60))
    keys = [k async for k in redis_cache.scan_iter(match="test:ratelimit:*")]
    assert len(keys) == 1
    ttl = await redis_cache.pttl(keys[0])
    assert 0 < ttl <= 120_000  # two windows: still needed as the next window's "previous"


class _StubRedis:
    """Just enough Redis for RateLimiter: register_script() hands back `script`."""

    def __init__(self, script: Callable[..., Awaitable[object]]) -> None:
        self._script = script

    def register_script(self, _: str) -> Callable[..., Awaitable[object]]:
        return self._script


async def _raises(**_: object) -> object:
    raise RedisError("down")


async def _hangs(**_: object) -> object:
    await asyncio.sleep(30)


async def test_fails_open_when_redis_errors() -> None:
    limiter = RateLimiter(cast(Redis, _StubRedis(_raises)), prefix="x")
    assert await limiter.hit("s", "c", Rate(1, 60)) is None


async def test_fails_open_when_redis_hangs() -> None:
    limiter = RateLimiter(cast(Redis, _StubRedis(_hangs)), prefix="x", timeout=0.05)
    started = time.perf_counter()
    assert await limiter.hit("s", "c", Rate(1, 60)) is None
    assert time.perf_counter() - started < 2  # bounded by the deadline, not by the hang


async def test_fails_open_when_redis_is_unreachable() -> None:
    dead = create_redis("redis://localhost:1/0")
    try:
        assert await RateLimiter(dead, prefix="x").hit("s", "c", Rate(1, 60)) is None
    finally:
        await dead.aclose()
