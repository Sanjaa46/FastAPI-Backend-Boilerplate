from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.dependencies import SessionDep, SettingsDep
from app.core.exceptions import ForbiddenError, InvalidTokenError, UnauthorizedError
from app.core.security import decode_token
from app.modules.auth.repository import RefreshTokenRepository
from app.modules.auth.service import AuthService
from app.modules.users.dependencies import UserServiceDep
from app.modules.users.exceptions import UserNotFoundError
from app.modules.users.schemas import UserRead

# auto_error=False: we raise our own 401 so the body matches the shared error envelope.
bearer_schema = HTTPBearer(auto_error=False, description="Access token from POST /auth/login")
def  get_auth_service(
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
        user = await users.get_user(claims.subject) # cached read (short TTL)
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

AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
CurrentUserDep = Annotated[UserRead, Depends(get_current_user)]
SuperuserDep = Annotated[UserRead, Depends(get_current_superuser)]