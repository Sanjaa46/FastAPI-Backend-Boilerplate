from typing import Annotated

from fastapi import Depends

from app.core.cache import CacheService
from app.core.dependencies import RedisCacheDep, SessionDep, SettingsDep
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService