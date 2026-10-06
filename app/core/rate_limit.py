"""Rate limiting: a Redis sliding-window limiter and the FastAPI dependencies built on it.

Algorithm (sliding window counter): every (scope, identifier) keeps one counter per fixed
window. The requests in the trailing window are estimated as

    previous_window_count * (share of that window still inside the trailing window)
    + current_window_count

That removes the 2x burst a plain fixed window allows at its boundary, yet costs two small
keys per client instead of one entry per request. Rejected requests are NOT counted, so a
client that honours `Retry-After` is let in again.

Contract (same as CacheService): a Redis failure never fails a request. The check is
skipped and logged, i.e. the limiter fails open. Every Redis call has a short deadline, so
a hung Redis cannot stall the API either.

Client IP: `request.client` is whatever uvicorn resolved. Behind a reverse proxy, set
FORWARDED_ALLOW_IPS to the proxy's address or network (NOT "*": uvicorn then trusts the
left-most X-Forwarded-For entry, which the client controls). Without it every user shares
the proxy's IP and therefore one bucket.

Usage on a route (rates come from Settings so they can be tuned without a deploy):

    limit_export = rate_limit("reports:export", lambda s: s.rate_limit_export, key=by_user)

    @router.post("/export", dependencies=[Depends(limit_export)])
"""

import asyncio
import ipaddress
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Rate, Settings
from app.core.dependencies import RedisCacheDep, SettingsDep
from app.core.exceptions import InvalidTokenError, TooManyRequestsError
from app.core.security import decode_token

log = structlog.get_logger()

# KEYS[1] counter of the current window, KEYS[2] counter of the previous window
# ARGV[1] limit, ARGV[2] window (ms), ARGV[3] now (ms)
# Returns {allowed (0/1), remaining, retry_after (ms)}. All arithmetic is scaled by the
# window length so it stays in exact integers.
_SLIDING_WINDOW = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local elapsed = tonumber(ARGV[3]) % window

local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local previous = tonumber(redis.call('GET', KEYS[2]) or '0')

local used = previous * (window - elapsed) + current * window
if used + window > limit * window then
    local retry
    if current >= limit then
        -- Full in this window: wait for the next one, then for the carried-over count to
        -- decay by one request's worth (window / limit).
        retry = (window - elapsed) + window / limit
    else
        retry = window - (limit - current - 1) * window / previous - elapsed
    end
    return {0, 0, math.ceil(retry)}
end

if redis.call('INCR', KEYS[1]) == 1 then
    redis.call('PEXPIRE', KEYS[1], window * 2)  -- must outlive the next window (its "previous")
end
return {1, math.floor((limit * window - used - window) / window), 0}
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int  # requests still allowed in the window, counting this one as spent
    retry_after: int  # seconds until a retry succeeds; 0 when allowed


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str,
        clock: Callable[[], float] = time.time,
        timeout: float = 0.25,
    ) -> None:
        self._script = redis.register_script(_SLIDING_WINDOW)
        self._prefix = prefix
        self._clock = clock
        # Runs on every request: a hung Redis must cost milliseconds, not the TCP timeout.
        self._timeout = timeout

    async def hit(self, scope: str, identifier: str, rate: Rate) -> RateLimitResult | None:
        """Count one request. Returns None when the check was skipped (Redis failure)."""
        window_ms = rate.window_seconds * 1000
        now_ms = int(self._clock() * 1000)
        index = now_ms // window_ms
        # The {..} hash tag keeps both counters in one slot should the cache ever be a cluster.
        base = f"{self._prefix}:{{{scope}:{identifier}}}"
        try:
            async with asyncio.timeout(self._timeout):
                allowed, remaining, retry_ms = await self._script(
                    keys=[f"{base}:{index}", f"{base}:{index - 1}"],
                    args=[rate.limit, window_ms, now_ms],
                )
        except (RedisError, TimeoutError):
            log.warning("rate_limit_check_failed", scope=scope, exc_info=True)
            return None
        return RateLimitResult(bool(allowed), rate.limit, remaining, math.ceil(retry_ms / 1000))


def get_rate_limiter(redis: RedisCacheDep, settings: SettingsDep) -> RateLimiter:
    return RateLimiter(redis, prefix=f"{settings.cache_prefix}:ratelimit")


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


# --------------------------------------------------------------------------- identifiers

# Returns who to count this request against, or None to skip the limit for it.
KeyFunc = Callable[[Request, Settings], str | None]


def client_ip(request: Request) -> str:
    """The caller's address, normalised so it cannot be dodged by switching addresses.

    IPv6 clients usually own a whole /64 (2**64 addresses), so they are bucketed by it.
    """
    host = request.client.host if request.client else "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host  # not an IP (e.g. a UNIX socket path): use as is
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(ip), 64), strict=False))
    return str(ip)


def by_ip(request: Request, settings: Settings) -> str | None:
    return client_ip(request)


def by_user(request: Request, settings: Settings) -> str | None:
    """The authenticated user's ID; None (limit skipped) without a valid access token.

    Only the token is checked, never the database: the limit must stay cheap. A forged or
    expired token falls through to the per-IP limits like any anonymous request.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        claims = decode_token(token.strip(), settings, expected_type="access")
    except InvalidTokenError:
        return None
    return str(claims.subject)


# --------------------------------------------------------------------------- dependencies


async def enforce(
    request: Request, limiter: RateLimiter, scope: str, identifier: str, rate: Rate
) -> None:
    """Count one request; raise TooManyRequestsError (429) when over the limit.

    The tightest limit that applied is kept on `request.state` for RateLimitHeadersMiddleware.
    """
    result = await limiter.hit(scope, identifier, rate)
    if result is None:
        return
    if not result.allowed:
        log.warning(
            "rate_limited", scope=scope, identifier=identifier, retry_after=result.retry_after
        )
        raise TooManyRequestsError(
            result.retry_after,
            headers={"X-RateLimit-Limit": str(result.limit), "X-RateLimit-Remaining": "0"},
        )
    current: RateLimitResult | None = getattr(request.state, "rate_limit", None)
    if current is None or result.remaining < current.remaining:
        request.state.rate_limit = result


def rate_limit(
    scope: str, rate: Callable[[Settings], Rate], key: KeyFunc = by_ip
) -> Callable[..., Awaitable[None]]:
    """Build a dependency that limits `rate` requests per `key` and `scope`.

    `scope` names the bucket (two routes with the same scope and key share one counter).
    Does nothing unless RATE_LIMIT_ENABLED=true.
    """

    async def dependency(request: Request, limiter: RateLimiterDep, settings: SettingsDep) -> None:
        if not settings.rate_limit_enabled:
            return
        identifier = key(request, settings)
        if identifier is not None:
            await enforce(request, limiter, scope, identifier, rate(settings))

    return dependency


# Umbrella limits for the whole API (see app/api/v1/router.py).
limit_ip = rate_limit("ip", lambda s: s.rate_limit_ip)
limit_user = rate_limit("user", lambda s: s.rate_limit_user, key=by_user)
