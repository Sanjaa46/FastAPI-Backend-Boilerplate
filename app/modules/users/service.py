import asyncio
from typing import Any
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.schemas import Page, PageParams
from app.core.cache import CacheService
from app.core.security import hash_password
from app.modules.users.exceptions import (
    EmailAlreadyExistsError,
    SelfModificationError,
    UserNotFoundError,
)
from app.modules.users.models import User
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import (
    AdminUserCreate,
    UserAdminUpdate,
    UserCreate,
    UserCredentials,
    UserRead,
    UserUpdateMe,
)
from app.modules.users.tasks import send