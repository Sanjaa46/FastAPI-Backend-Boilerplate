"""API tests for the users module (self-service, admin management, caching)."""

import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from redis.exceptions import RedisError

from tests.conftest import PASSWORD, LoginFn, UserFactory, bearer

USERS = "/api/v1/users"
ME = f"{USERS}/me"


async def _admin_headers(make_user: UserFactory, login: LoginFn) -> dict[str, str]:
    admin = await make_user("admin@example.com", is_superuser=True)
    return bearer(await login(admin.email))


# --------------------------------------------------------------------------- self-service


async def test_update_me(client: AsyncClient, make_user: UserFactory, login: LoginFn) -> None:
    await make_user("u@example.com")
    headers = bearer(await login("u@example.com"))

    response = await client.patch(ME, json={"full_name": "  Renamed  "}, headers=headers)
    assert response.status_code == 200
    assert response.json()["full_name"] == "Renamed"
    # The change is visible on the next read (cache was invalidated).
    assert (await client.get(ME, headers=headers)).json()["full_name"] == "Renamed"


async def test_update_me_cannot_escalate_privileges(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    headers = bearer(await login("u@example.com"))
    for field, value in (("is_superuser", True), ("is_active", False), ("email", "x@example.com")):
        response = await client.patch(ME, json={field: value}, headers=headers)
        assert response.status_code == 422, field


# --------------------------------------------------------------------------- admin: authorization


async def test_admin_routes_require_superuser(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    headers = bearer(await login("u@example.com"))
    some_id = uuid.uuid4()

    checks = [
        await client.get(USERS, headers=headers),
        await client.post(USERS, json={}, headers=headers),
        await client.get(f"{USERS}/{some_id}", headers=headers),
        await client.patch(f"{USERS}/{some_id}", json={}, headers=headers),
        await client.delete(f"{USERS}/{some_id}", headers=headers),
    ]
    assert [r.status_code for r in checks] == [403] * 5
    assert checks[0].json()["error"]["code"] == "forbidden"
    assert (await client.get(USERS)).status_code == 401  # anonymous


# --------------------------------------------------------------------------- admin: CRUD


async def test_admin_list_is_paginated(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    headers = await _admin_headers(make_user, login)
    for i in range(4):
        await make_user(f"member{i}@example.com")

    page = await client.get(USERS, params={"limit": 2, "offset": 0}, headers=headers)
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 5 and body["limit"] == 2 and body["offset"] == 0
    assert len(body["items"]) == 2
    assert all("password_hash" not in item for item in body["items"])

    last = await client.get(USERS, params={"limit": 2, "offset": 4}, headers=headers)
    assert len(last.json()["items"]) == 1

    # Pages do not overlap.
    second = await client.get(USERS, params={"limit": 2, "offset": 2}, headers=headers)
    ids = [u["id"] for u in body["items"]] + [u["id"] for u in second.json()["items"]]
    assert len(set(ids)) == 4


@pytest.mark.parametrize("params", [{"limit": 101}, {"limit": 0}, {"offset": -1}])
async def test_admin_list_rejects_bad_pagination(
    client: AsyncClient, make_user: UserFactory, login: LoginFn, params: dict[str, int]
) -> None:
    headers = await _admin_headers(make_user, login)
    assert (await client.get(USERS, params=params, headers=headers)).status_code == 422


async def test_admin_create_user_with_flags(
    client: AsyncClient, make_user: UserFactory, login: LoginFn, welcome_email_kiq: AsyncMock
) -> None:
    headers = await _admin_headers(make_user, login)
    payload = {
        "email": "Second.Admin@example.com",
        "password": PASSWORD,
        "full_name": "Second Admin",
        "is_superuser": True,
    }
    created = await client.post(USERS, json=payload, headers=headers)
    assert created.status_code == 201
    assert created.json()["is_superuser"] is True
    welcome_email_kiq.assert_awaited_once()

    # The new admin can log in and use admin routes.
    tokens = await login("second.admin@example.com")
    assert (await client.get(USERS, headers=bearer(tokens))).status_code == 200

    duplicate = await client.post(USERS, json=payload, headers=headers)
    assert duplicate.status_code == 409


async def test_admin_get_user(client: AsyncClient, make_user: UserFactory, login: LoginFn) -> None:
    headers = await _admin_headers(make_user, login)
    target = await make_user("target@example.com")

    ok = await client.get(f"{USERS}/{target.id}", headers=headers)
    assert ok.status_code == 200 and ok.json()["email"] == "target@example.com"

    missing = await client.get(f"{USERS}/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "user_not_found"

    assert (await client.get(f"{USERS}/not-a-uuid", headers=headers)).status_code == 422
    # "me" is routed to the self-service endpoint, not parsed as a UUID.
    assert (await client.get(ME, headers=headers)).status_code == 200


async def test_admin_update_user(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    headers = await _admin_headers(make_user, login)
    target = await make_user("target@example.com")

    response = await client.patch(
        f"{USERS}/{target.id}",
        json={"full_name": "Promoted", "is_superuser": True},
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["full_name"] == "Promoted" and body["is_superuser"] is True
    assert body["email"] == "target@example.com"  # untouched fields are preserved

    missing = await client.patch(f"{USERS}/{uuid.uuid4()}", json={}, headers=headers)
    assert missing.status_code == 404


async def test_admin_cannot_lock_themselves_out(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    admin = await make_user("admin@example.com", is_superuser=True)
    headers = bearer(await login(admin.email))

    for body in ({"is_active": False}, {"is_superuser": False}):
        response = await client.patch(f"{USERS}/{admin.id}", json=body, headers=headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "self_modification_forbidden"
    assert (await client.delete(f"{USERS}/{admin.id}", headers=headers)).status_code == 400
    # Harmless self-edits are fine.
    ok = await client.patch(f"{USERS}/{admin.id}", json={"full_name": "Boss"}, headers=headers)
    assert ok.status_code == 200


async def test_admin_delete_user_kills_their_sessions(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    headers = await _admin_headers(make_user, login)
    victim = await make_user("victim@example.com")
    victim_tokens = await login(victim.email)
    assert (await client.get(ME, headers=bearer(victim_tokens))).status_code == 200  # warms cache

    deleted = await client.delete(f"{USERS}/{victim.id}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"{USERS}/{victim.id}", headers=headers)).status_code == 404
    # The access token outlived its user: rejected. Refresh rows were cascade-deleted.
    assert (await client.get(ME, headers=bearer(victim_tokens))).status_code == 401
    refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": victim_tokens["refresh_token"]}
    )
    assert refresh.status_code == 401


# --------------------------------------------------------------------------- caching


async def test_user_reads_are_cached_and_invalidated_on_write(
    client: AsyncClient,
    make_user: UserFactory,
    login: LoginFn,
    redis_cache: Redis,
) -> None:
    headers = await _admin_headers(make_user, login)
    target = await make_user("target@example.com")
    key = f"app:users:v1:{target.id}"

    assert await redis_cache.exists(key) == 0
    assert (await client.get(f"{USERS}/{target.id}", headers=headers)).status_code == 200
    assert await redis_cache.exists(key) == 1  # cached on first read
    assert await redis_cache.ttl(key) > 0  # every key has a TTL

    patched = await client.patch(f"{USERS}/{target.id}", json={"full_name": "New"}, headers=headers)
    assert patched.status_code == 200
    assert await redis_cache.exists(key) == 0  # invalidated after commit
    assert (await client.get(f"{USERS}/{target.id}", headers=headers)).json()["full_name"] == "New"


async def test_cache_never_stores_password_hash(
    client: AsyncClient, make_user: UserFactory, login: LoginFn, redis_cache: Redis
) -> None:
    user = await make_user("u@example.com")
    await client.get(ME, headers=bearer(await login(user.email)))
    cached = await redis_cache.get(f"app:users:v1:{user.id}")
    assert cached is not None and "password" not in cached


async def test_requests_survive_a_cache_outage(
    client: AsyncClient,
    make_user: UserFactory,
    login: LoginFn,
    redis_cache: Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await make_user("u@example.com")
    headers = bearer(await login(user.email))

    for method in ("get", "set", "delete"):
        monkeypatch.setattr(redis_cache, method, AsyncMock(side_effect=RedisError("down")))

    assert (await client.get(ME, headers=headers)).status_code == 200
    renamed = await client.patch(ME, json={"full_name": "Still Works"}, headers=headers)
    assert renamed.status_code == 200
