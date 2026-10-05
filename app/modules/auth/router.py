"""HTTP surface for authentication. No SQL, no business logic."""

from fastapi import APIRouter, Response, status

from app.common.schemas import error_responses
from app.modules.auth.dependencies import AuthServiceDep, CurrentUserDep
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    TokenPair,
)
from app.modules.users.schemas import UserCreate, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(403, 409, 422),
)
async def register(payload: UserCreate, service: AuthServiceDep) -> UserRead:
    """Create an account. Disabled when REGISTRATION_ENABLED=false."""
    return await service.register(payload)


@router.post("/login", response_model=TokenPair, responses=error_responses(401, 403, 422))
async def login(payload: LoginRequest, service: AuthServiceDep) -> TokenPair:
    """Exchange email + password for an access/refresh token pair."""
    return await service.login(payload.email, payload.password)


@router.post("/refresh", response_model=TokenPair, responses=error_responses(401, 403, 422))
async def refresh(payload: RefreshRequest, service: AuthServiceDep) -> TokenPair:
    """Rotate a refresh token. The presented token becomes unusable; replaying it ends the
    session."""
    return await service.refresh(payload.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, responses=error_responses(422))
async def logout(payload: RefreshRequest, service: AuthServiceDep) -> Response:
    """End the session behind this refresh token. Always 204 (idempotent)."""
    await service.logout(payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/logout-all", status_code=status.HTTP_204_NO_CONTENT, responses=error_responses(401, 403)
)
async def logout_all(user: CurrentUserDep, service: AuthServiceDep) -> Response:
    """End every session of the current user."""
    await service.logout_all(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/change-password", response_model=TokenPair, responses=error_responses(400, 401, 403, 422)
)
async def change_password(
    payload: ChangePasswordRequest, user: CurrentUserDep, service: AuthServiceDep
) -> TokenPair:
    """Change the password. All other sessions end; the response is a fresh token pair."""
    return await service.change_password(user.id, payload.current_password, payload.new_password)
