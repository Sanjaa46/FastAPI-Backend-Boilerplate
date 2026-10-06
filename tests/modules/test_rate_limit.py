"""API tests for rate limiting: 429 envelope, headers, and each limit's key (IP, user, email)."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient, Response
from redis.exceptions import RedisError

from app.core.config import Rate, Settings, get_settings
from app.core.redis import get_redis_cache
from app.main import app
from tests.conftest import PASSWORD, LoginFn, UserFactory, bearer

LOGIN = "/api/v1/auth/login"
REGISTER = "/api/v1/auth/register"
CHANGE_PASSWORD = "/api/v1/auth/change-password"
ME = "/api/v1/users/me"
BAD_PASSWORD = "wrong-password"

# Out of the way unless a test tightens one of them.
_GENEROUS = {
    name: Rate(1000, 60)
    for name in (
        "rate_limit_ip",
        "rate_limit_user",
        "rate_limit_login",
        "rate_limit_login_account",
        "rate_limit_register",
        "rate_limit_change_password",
    )
}

Limits = Callable[..., None]


@pytest.fixture
def limits(settings: Settings) -> Limits:
    """Switch rate limiting on; `limits(rate_limit_ip=Rate(3, 60))` tightens a single limit."""

    def _apply(**tight: Rate) -> None:
        patched = settings.model_copy(update={"rate_limit_enabled": True, **_GENEROUS, **tight})
        app.dependency_overrides[get_settings] = lambda: patched

    return _apply


@asynccontextmanager
async def client_from(ip: str, **headers: str) -> AsyncIterator[AsyncClient]:
    """A client whose connection comes from `ip` (the `client` fixture is always 127.0.0.1)."""
    transport = ASGITransport(app=app, client=(ip, 4321))
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


async def _login(client: AsyncClient, email: str, password: str = BAD_PASSWORD) -> Response:
    return await client.post(LOGIN, json={"email": email, "password": password})


# --------------------------------------------------------------------------- off by default


async def test_disabled_by_default_adds_no_limits_or_headers(client: AsyncClient) -> None:
    responses = [await client.get(ME) for _ in range(10)]
    assert {r.status_code for r in responses} == {401}
    assert "x-ratelimit-limit" not in responses[-1].headers


# --------------------------------------------------------------------------- per IP


async def test_ip_limit_returns_429_in_the_error_envelope(
    client: AsyncClient, limits: Limits
) -> None:
    limits(rate_limit_ip=Rate(3, 60))

    allowed = [await client.get(ME) for _ in range(3)]
    assert [r.status_code for r in allowed] == [401, 401, 401]  # counted even when the route fails
    assert [r.headers["x-ratelimit-remaining"] for r in allowed] == ["2", "1", "0"]
    assert allowed[0].headers["x-ratelimit-limit"] == "3"

    blocked = await client.get(ME, headers={"X-Request-ID": "req-429"})
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["error"]["code"] == "rate_limited"
    assert body["error"]["details"]["retry_after"] == int(blocked.headers["retry-after"]) > 0
    assert body["request_id"] == "req-429"
    assert blocked.headers["x-ratelimit-limit"] == "3"
    assert blocked.headers["x-ratelimit-remaining"] == "0"


async def test_ip_limit_is_per_address(client: AsyncClient, limits: Limits) -> None:
    limits(rate_limit_ip=Rate(2, 60))
    assert [(await client.get(ME)).status_code for _ in range(3)] == [401, 401, 429]

    async with client_from("203.0.113.9") as other:
        assert (await other.get(ME)).status_code == 401  # a different caller is unaffected


async def test_client_supplied_forwarding_headers_cannot_dodge_the_limit(
    client: AsyncClient, limits: Limits
) -> None:
    limits(rate_limit_ip=Rate(2, 60))
    statuses = [
        (await client.get(ME, headers={"X-Forwarded-For": f"198.51.100.{n}"})).status_code
        for n in range(4)
    ]
    assert statuses == [401, 401, 429, 429]


async def test_health_probes_are_not_rate_limited(client: AsyncClient, limits: Limits) -> None:
    limits(rate_limit_ip=Rate(1, 60))
    assert (await client.get(ME)).status_code == 401
    assert (await client.get(ME)).status_code == 429
    assert (await client.get("/health/live")).status_code == 200


async def test_the_tightest_limit_is_the_one_reported(
    client: AsyncClient, limits: Limits, make_user: UserFactory, login: LoginFn
) -> None:
    limits(rate_limit_ip=Rate(100, 60), rate_limit_user=Rate(5, 60))
    await make_user("tight@example.com")
    tokens = await login("tight@example.com")
    response = await client.get(ME, headers=bearer(tokens))
    assert response.status_code == 200
    assert response.headers["x-ratelimit-limit"] == "5"  # the user limit, not the IP one
    assert response.headers["x-ratelimit-remaining"] == "4"


# --------------------------------------------------------------------------- per user


async def test_user_limit_follows_the_user_not_the_address(
    client: AsyncClient, limits: Limits, make_user: UserFactory, login: LoginFn
) -> None:
    limits(rate_limit_user=Rate(2, 60))
    await make_user("alice@example.com")
    await make_user("bob@example.com")
    alice, bob = await login("alice@example.com"), await login("bob@example.com")

    assert [(await client.get(ME, headers=bearer(alice))).status_code for _ in range(3)] == [
        200,
        200,
        429,
    ]
    # Alice from another address is still Alice ...
    async with client_from("203.0.113.50") as elsewhere:
        assert (await elsewhere.get(ME, headers=bearer(alice))).status_code == 429
    # ... while Bob, on the very same address as before, is not throttled.
    assert (await client.get(ME, headers=bearer(bob))).status_code == 200


async def test_user_limit_ignores_anonymous_and_forged_tokens(
    client: AsyncClient, limits: Limits
) -> None:
    limits(rate_limit_user=Rate(1, 60))
    anonymous = [await client.get(ME) for _ in range(3)]
    forged = [await client.get(ME, headers={"Authorization": "Bearer nope"}) for _ in range(3)]
    assert {r.status_code for r in anonymous + forged} == {401}  # never 429


# --------------------------------------------------------------------------- auth routes


async def test_login_is_limited_per_ip(client: AsyncClient, limits: Limits) -> None:
    limits(rate_limit_login=Rate(2, 60))
    statuses = [(await _login(client, f"user{n}@example.com")).status_code for n in range(3)]
    assert statuses == [401, 401, 429]

    async with client_from("203.0.113.9") as other:
        assert (await _login(other, "user9@example.com")).status_code == 401


async def test_login_is_limited_per_account_across_addresses(
    client: AsyncClient, limits: Limits, make_user: UserFactory
) -> None:
    limits(rate_limit_login_account=Rate(2, 60))
    await make_user("victim@example.com")

    for ip in ("203.0.113.1", "203.0.113.2"):
        async with client_from(ip) as attacker:
            assert (await _login(attacker, "victim@example.com")).status_code == 401
    async with client_from("203.0.113.3") as third:
        # Over the account's budget, even with the right password and a fresh address.
        blocked = await _login(third, "Victim@Example.com", PASSWORD)  # case-insensitive
        assert blocked.status_code == 429
        assert (await _login(third, "someone-else@example.com")).status_code == 401


async def test_login_still_validates_the_body_first(client: AsyncClient, limits: Limits) -> None:
    limits(rate_limit_login_account=Rate(1, 60))
    for _ in range(3):
        response = await client.post(LOGIN, json={"email": "not-an-email", "password": "x"})
        assert response.status_code == 422  # invalid bodies never consume the account budget


async def test_register_is_limited_per_ip(client: AsyncClient, limits: Limits) -> None:
    limits(rate_limit_register=Rate(1, 3600))
    first = await client.post(
        REGISTER, json={"email": "a@example.com", "password": PASSWORD, "full_name": "A"}
    )
    second = await client.post(
        REGISTER, json={"email": "b@example.com", "password": PASSWORD, "full_name": "B"}
    )
    assert (first.status_code, second.status_code) == (201, 429)
    assert int(second.headers["retry-after"]) > 60  # the window is an hour


async def test_change_password_is_limited_per_user(
    client: AsyncClient, limits: Limits, make_user: UserFactory, login: LoginFn
) -> None:
    limits(rate_limit_change_password=Rate(1, 60))
    await make_user("pw@example.com")
    headers = bearer(await login("pw@example.com"))
    body = {"current_password": "not-my-password", "new_password": "Another-Pa55word!"}

    first = await client.post(CHANGE_PASSWORD, json=body, headers=headers)
    second = await client.post(CHANGE_PASSWORD, json=body, headers=headers)
    assert (first.status_code, second.status_code) == (400, 429)


async def test_umbrella_429_is_documented_in_openapi(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert "429" in schema["paths"][LOGIN]["post"]["responses"]
    assert "429" in schema["paths"][ME]["get"]["responses"]


# --------------------------------------------------------------------------- failure mode


class _DownRedis:
    def register_script(self, _: str) -> Callable[..., Awaitable[object]]:
        async def script(**_: object) -> object:
            raise RedisError("down")

        return script


async def test_redis_outage_fails_open(client: AsyncClient, limits: Limits) -> None:
    limits(rate_limit_ip=Rate(1, 60))
    app.dependency_overrides[get_redis_cache] = lambda: _DownRedis()
    responses = [await client.get(ME) for _ in range(3)]
    assert [r.status_code for r in responses] == [401, 401, 401]  # served, not 429 / 500
    assert "x-ratelimit-limit" not in responses[0].headers
