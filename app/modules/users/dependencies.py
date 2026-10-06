from typing import Annotated

from fastapi import Depends

from app.core.cache import CacheService
from app.core.dependencies import RedisCacheDep, SessionDep, SettingsDep
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService


def get_user_service(
    session: SessionDep,
    redis: RedisCacheDep,
    settings: SettingsDep,
) -> UserService:
    cache = CacheService(
        redis,
        prefix=f"{settings.cache_prefix}:users",
        default_ttl=settings.cache_default_ttl_seconds,
    )
    return UserService(UserRepository(session), cache, session)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
