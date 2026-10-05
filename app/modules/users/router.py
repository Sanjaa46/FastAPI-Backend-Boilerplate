"""HTTP surface for users. No SQL, no business logic.

`/me` routes are declared BEFORE `/{user_id}` so "me" is never parsed as a UUID.
"""

from uuid import UUID

from fastapi import APIRouter, Response, status

from app.common.schemas import Page, PageParams, error_responses
from app.modules.auth.dependencies import CurrentUserDep, SuperuserDep
from app.modules.users.dependencies import UserServiceDep
from app.modules.users.schemas import AdminUserCreate, UserAdminUpdate, UserRead, UserUpdateMe

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead, responses=error_responses(401, 403))
async def read_me(user: CurrentUserDep) -> UserRead:
    return user


@router.patch("/me", response_model=UserRead, responses=error_responses(401, 403, 422))
async def update_me(
    payload: UserUpdateMe, user: CurrentUserDep, service: UserServiceDep
) -> UserRead:
    return await service.update_profile(user.id, payload)


@router.get("", response_model=Page[UserRead], responses=error_responses(401, 403))
async def list_users(
    _: SuperuserDep, service: UserServiceDep, params: PageParams
) -> Page[UserRead]:
    return await service.list_users(params)


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(401, 403, 409, 422),
)
async def create_user(
    payload: AdminUserCreate, _: SuperuserDep, service: UserServiceDep
) -> UserRead:
    return await service.create_user(payload)


@router.get("/{user_id}", response_model=UserRead, responses=error_responses(401, 403, 404, 422))
async def get_user(user_id: UUID, _: SuperuserDep, service: UserServiceDep) -> UserRead:
    """(Admin) Fetch a user by ID."""
    return await service.get_user(user_id)


@router.patch(
    "/{user_id}", response_model=UserRead, responses=error_responses(400, 401, 403, 404, 422)
)
async def update_user(
    user_id: UUID, payload: UserAdminUpdate, admin: SuperuserDep, service: UserServiceDep
) -> UserRead:
    """(Admin) Update a user. Admins cannot deactivate or demote themselves."""
    return await service.admin_update_user(user_id, payload, actor_id=admin.id)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(400, 401, 403, 404, 422),
)
async def delete_user(user_id: UUID, admin: SuperuserDep, service: UserServiceDep) -> Response:
    """(Admin) Permanently delete a user and their sessions. Admins cannot delete themselves."""
    await service.delete_user(user_id, actor_id=admin.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
