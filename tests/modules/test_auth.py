"""API tests for the auth module (register, login, refresh rotation, logout, password change)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.main import app
from app.modules.auth.models import RefreshToken
from app.modules.auth.tasks import purge_expired_refresh_tokens
from tests.conftest import PASSWORD, LoginFn, UserFactory, bearer

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
LOGOUT_ALL = "/api/v1/auth/logout-all"
CHANGE_PASSWORD = "/api/v1/auth/change-password"
ME = "/api/v1/users/me"


# --------------------------------------------------------------------------- register


async def test_register_creates_user_and_enqueues_welcome_email(
    client: AsyncClient, welcome_email_kiq: AsyncMock
) -> None:
    response = await client.post(
        REGISTER,
        json={"email": "New.User@Example.com", "password": PASSWORD, "full_name": "  New User "},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new.user@example.com"  # normalized
    assert body["full_name"] == "New User"  # trimmed
    assert body["is_active"] is True
    assert body["is_superuser"] is False
    assert "password" not in body and "password_hash" not in body
    welcome_email_kiq.assert_awaited_once_with(body["id"])


async def test_register_welcome_email_failure_does_not_fail_registration(
    client: AsyncClient, welcome_email_kiq: AsyncMock
) -> None:
    welcome_email_kiq.side_effect = ConnectionError("broker down")
    response = await client.post(
        REGISTER, json={"email": "a@example.com", "password": PASSWORD, "full_name": "A"}
    )
    assert response.status_code == 201


async def test_register_duplicate_email_is_case_insensitive(
    client: AsyncClient, make_user: UserFactory
) -> None:
    await make_user("taken@example.com")
    response = await client.post(
        REGISTER, json={"email": "TAKEN@example.com", "password": PASSWORD, "full_name": "X"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_exists"


async def test_register_validation_error_shape_and_no_secret_echo(client: AsyncClient) -> None:
    response = await client.post(
        REGISTER, json={"email": "not-an-email", "password": "Sh0rt!pw", "full_name": "X"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["request_id"]
    fields = {tuple(d["loc"]) for d in body["error"]["details"]}
    assert ("body", "email") in fields and ("body", "password") in fields
    assert "Sh0rt!pw" not in response.text  # the submitted password is never echoed back


async def test_register_rejects_privilege_fields(client: AsyncClient) -> None:
    response = await client.post(
        REGISTER,
        json={
            "email": "a@example.com",
            "password": PASSWORD,
            "full_name": "A",
            "is_superuser": True,
        },
    )
    assert response.status_code == 422


async def test_register_disabled(client: AsyncClient, settings: Settings) -> None:
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"registration_enabled": False}
    )
    response = await client.post(
        REGISTER, json={"email": "a@example.com", "password": PASSWORD, "full_name": "A"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "registration_disabled"


# --------------------------------------------------------------------------- login


async def test_login_success_and_access_token_works(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    tokens = await login("U@Example.com")  # email is case-insensitive
    assert tokens["token_type"] == "bearer"
    assert int(tokens["expires_in"]) > 0
    me = await client.get(ME, headers=bearer(tokens))
    assert me.status_code == 200
    assert me.json()["email"] == "u@example.com"


async def test_login_failures_are_indistinguishable(
    client: AsyncClient, make_user: UserFactory
) -> None:
    await make_user("u@example.com")
    wrong_password = await client.post(LOGIN, json={"email": "u@example.com", "password": "nope"})
    unknown_email = await client.post(
        LOGIN, json={"email": "ghost@example.com", "password": "nope"}
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["error"] == unknown_email.json()["error"]
    assert wrong_password.json()["error"]["code"] == "invalid_credentials"
    assert wrong_password.headers["www-authenticate"] == "Bearer"


async def test_login_inactive_user_forbidden(client: AsyncClient, make_user: UserFactory) -> None:
    await make_user("off@example.com", is_active=False)
    response = await client.post(LOGIN, json={"email": "off@example.com", "password": PASSWORD})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "inactive_user"


# --------------------------------------------------------------------------- bearer auth


async def test_protected_route_requires_valid_access_token(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    tokens = await login("u@example.com")

    missing = await client.get(ME)
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"

    garbage = await client.get(ME, headers={"Authorization": "Bearer nonsense"})
    assert garbage.status_code == 401
    assert garbage.json()["error"]["code"] == "invalid_token"

    # A refresh token must not be accepted as an access token.
    wrong_type = await client.get(
        ME, headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )
    assert wrong_type.status_code == 401


# --------------------------------------------------------------------------- refresh


async def test_refresh_rotates_and_detects_reuse(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    first = await login("u@example.com")

    rotated = await client.post(REFRESH, json={"refresh_token": first["refresh_token"]})
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (await client.get(ME, headers=bearer(second))).status_code == 200

    # Replaying the already-used token is rejected AND burns the whole family...
    replay = await client.post(REFRESH, json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401
    # ...including the legitimately rotated token.
    after = await client.post(REFRESH, json={"refresh_token": second["refresh_token"]})
    assert after.status_code == 401


async def test_refresh_rejects_access_token_and_garbage(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    tokens = await login("u@example.com")
    for bad in (tokens["access_token"], "garbage"):
        response = await client.post(REFRESH, json={"refresh_token": bad})
        assert response.status_code == 401


async def test_refresh_rejected_for_deactivated_user(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    admin = await make_user("admin@example.com", is_superuser=True)
    victim = await make_user("victim@example.com")
    admin_tokens = await login(admin.email)
    victim_tokens = await login(victim.email)

    patch = await client.patch(
        f"/api/v1/users/{victim.id}", json={"is_active": False}, headers=bearer(admin_tokens)
    )
    assert patch.status_code == 200

    assert (await client.get(ME, headers=bearer(victim_tokens))).status_code == 403
    refresh = await client.post(REFRESH, json={"refresh_token": victim_tokens["refresh_token"]})
    assert refresh.status_code == 403


# --------------------------------------------------------------------------- logout


async def test_logout_revokes_session_and_is_idempotent(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    tokens = await login("u@example.com")

    assert (
        await client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 204
    assert (
        await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 401
    # Second logout and a garbage token both succeed silently.
    assert (
        await client.post(LOGOUT, json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 204
    assert (await client.post(LOGOUT, json={"refresh_token": "garbage"})).status_code == 204


async def test_logout_all_revokes_every_session(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    phone = await login("u@example.com")
    laptop = await login("u@example.com")

    assert (await client.post(LOGOUT_ALL, headers=bearer(phone))).status_code == 204
    for tokens in (phone, laptop):
        response = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
        assert response.status_code == 401
    assert (await client.post(LOGOUT_ALL)).status_code == 401  # needs authentication


# --------------------------------------------------------------------------- change password


async def test_change_password_rotates_everything(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    this_device = await login("u@example.com")
    other_device = await login("u@example.com")

    wrong = await client.post(
        CHANGE_PASSWORD,
        json={"current_password": "wrong-password", "new_password": "a-brand-new-password"},
        headers=bearer(this_device),
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "invalid_current_password"

    changed = await client.post(
        CHANGE_PASSWORD,
        json={"current_password": PASSWORD, "new_password": "a-brand-new-password"},
        headers=bearer(this_device),
    )
    assert changed.status_code == 200
    fresh = changed.json()
    assert (await client.get(ME, headers=bearer(fresh))).status_code == 200

    # Old sessions are dead, the old password no longer works, the new one does.
    for tokens in (this_device, other_device):
        response = await client.post(REFRESH, json={"refresh_token": tokens["refresh_token"]})
        assert response.status_code == 401
    old = await client.post(LOGIN, json={"email": "u@example.com", "password": PASSWORD})
    assert old.status_code == 401
    new = await client.post(
        LOGIN, json={"email": "u@example.com", "password": "a-brand-new-password"}
    )
    assert new.status_code == 200
    # ...and the fresh session from the change-password response can still be refreshed.
    again = await client.post(REFRESH, json={"refresh_token": fresh["refresh_token"]})
    assert again.status_code == 200


async def test_change_password_enforces_policy(
    client: AsyncClient, make_user: UserFactory, login: LoginFn
) -> None:
    await make_user("u@example.com")
    tokens = await login("u@example.com")
    response = await client.post(
        CHANGE_PASSWORD,
        json={"current_password": PASSWORD, "new_password": "short"},
        headers=bearer(tokens),
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- cleanup task


async def test_purge_task_deletes_only_expired_tokens(
    client: AsyncClient, make_user: UserFactory, login: LoginFn, session: AsyncSession
) -> None:
    user = await make_user("u@example.com")
    await login("u@example.com")  # one live token
    expired = RefreshToken(
        user_id=user.id,
        family_id=user.id,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(expired)
    await session.commit()

    deleted = await purge_expired_refresh_tokens(session)

    assert deleted == 1
    remaining = (await session.execute(select(RefreshToken))).scalars().all()
    assert len(remaining) == 1 and remaining[0].id != expired.id
