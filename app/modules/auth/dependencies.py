import hashlib
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.dependencies import SessionDep, SettingsDep
from app.core.exceptions import ForbiddenError, InvalidTokenError, UnauthorizedError
from app.core.rate_limit import RateLimiterDep, by_user, enforce, rate_limit
from app.core.security import decode_token
from app.modules.auth.repository import RefreshTokenRepository
from app.modules.auth.schemas import LoginRequest
from app.modules.auth.service import AuthService
from app.modules.users.dependencies import UserServiceDep
from app.modules.users.exceptions import UserNotFoundError
from app.modules.users.schemas import UserRead

# auto_error=False: we raise our own 401 so the body matches the shared error envelope.
bearer_schema = HTTPBearer(auto_error=False, description="Access token from POST /auth/login")


def get_auth_service(
    users: UserServiceDep, session: SessionDep, settings: SettingsDep
) -> AuthService:
    return AuthService(users, RefreshTokenRepository(session), session, settings)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_schema)],
    settings: SettingsDep,
    users: UserServiceDep,
) -> UserRead:
    """Resolve the Bearer access token to an active user (401 / 403 otherwise)."""
    if credentials is None:
        raise UnauthorizedError()
    claims = decode_token(credentials.credentials, settings, expected_type="access")
    try:
        user = await users.get_user(claims.subject)  # cached read (short TTL)
    except UserNotFoundError as exc:
        raise InvalidTokenError from exc
    if not user.is_active:
        raise ForbiddenError("This account is disabled")
    return user


async def get_current_superuser(
    user: Annotated[UserRead, Depends(get_current_user)],
) -> UserRead:
    """Like get_current_user, but additionally requires is_superuser."""
    if not user.is_superuser:
        raise ForbiddenError("Administrator privilages required")
    return user


# Stricter limits on top of the umbrella ones (they only apply with RATE_LIMIT_ENABLED=true).
limit_login = rate_limit("auth:login", lambda s: s.rate_limit_login)
limit_register = rate_limit("auth:register", lambda s: s.rate_limit_register)
limit_change_password = rate_limit(
    "auth:change-password", lambda s: s.rate_limit_change_password, key=by_user
)


async def get_login_payload(
    payload: LoginRequest, request: Request, limiter: RateLimiterDep, settings: SettingsDep
) -> LoginRequest:
    """The login body, after counting the attempt against the targeted account.

    Per email on top of per IP: a botnet guessing one account's password is caught although
    every address stays under its own limit. Trade-off: someone who knows an email can burn
    its budget and delay that user's logins for the window.
    """
    if settings.rate_limit_enabled:
        email_hash = hashlib.sha256(payload.email.strip().lower().encode()).hexdigest()
        await enforce(
            request, limiter, "auth:login-account", email_hash, settings.rate_limit_login_account
        )
    return payload


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
LoginPayloadDep = Annotated[LoginRequest, Depends(get_login_payload)]
CurrentUserDep = Annotated[UserRead, Depends(get_current_user)]
SuperuserDep = Annotated[UserRead, Depends(get_current_superuser)]
