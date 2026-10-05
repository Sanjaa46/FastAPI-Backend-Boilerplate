import asyncio
from typing import Any
from uuid import UUID

import structlog
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
from app.modules.users.tasks import send_welcome_email

log = structlog.get_logger()


# Short TTL on purpose: the auth layer reads `is_active` from this cache on every request.
# Invalidation after writes is the primary mechanism; the TTL bounds staleness if a
# delete is lost (Redis outage).
USER_CACHE_TTL_SECONDS = 60


class UserService:
    """Owns the transaction for every users use case (repositories only flush)."""

    def __init__(self, repo: UserRepository, cache: CacheService, session: AsyncSession) -> None:
        self._repo = repo
        self._cache = cache
        self._session = session


    # --------------------------------------- helpers

    def _key(self, user_id: UUID) -> str:
        return self._cache.key("v1", str(user_id))

    async def _get_or_raise(self, user_id: UUID) -> User:
        user = await self._repo.get(user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        return user

    async def _apply_changes(self, user_id: UUID, changes: dict[str, Any]) -> UserRead:
        """Apply already-validated field changes, commit, then invalidate the cache."""
        user = await self._get_or_raise(user_id)
        for field, value in changes.items():
            setattr(user, field, value)
        await self._session.commit()
        await self._cache.delete(self._key(user_id))
        return UserRead.model_validate(user)

    async def _enqueue_welcome_email(self, user_id: UUID) -> None:
        """Best effort: a broker outage must not fail a registration that already committed."""
        try:
            # taskiq's stubs put injected dependencies (the session) into kiq()'s signature,
            # although callers never pass them: hence the ignore.
            await send_welcome_email.kiq(str(user_id))  # type: ignore[call-overload]
        except Exception:
            log.warning("welcome_email_enqueue_failed", user_id=str(user_id), exc_info=True)
    # --------------------------------------------- queries

    async def get_user(self, user_id: UUID) -> UserRead:
        """Cached read. Raise UserNotFoundError (never cached)."""

        async def load() -> UserRead:
            return UserRead.model_validate(await self._get_or_raise(user_id))

        return await self._cache.get_or_load(
            self._key(user_id), UserRead, load, ttl=USER_CACHE_TTL_SECONDS
        )

    async def list_users(self, params: PageParams) -> Page[UserRead]:
        users = await self._repo.list_page(limit=params.limit, offset=params.offset)
        return Page[UserRead](
            items=[UserRead.model_validate(u) for u in users],
            total=await self._repo.count(),
            limit=params.limit,
            offset=params.offset,
        )

    async def get_credentials_by_email(self, email: str) -> UserCredentials | None:
        """INTERNAL (auth module): the password hash for a login attempt. Never cached."""
        user = await self._repo.get_by_email(email=email.strip().lower())
        return None if user is None else UserCredentials.model_validate(user)

    async def get_credentials(self, user_id: UUID) -> UserCredentials | None:
        """INTERNAL (auth module): the password hash for an existing user. Never cached."""
        user = await self._repo.get(user_id)
        return None if user is None else UserCredentials.model_validate(user)

    # -------------------------------------------- commands

    async def create_user(self, payload: UserCreate | AdminUserCreate) -> UserRead:
        if await self._repo.get_by_email(payload.email) is not None:
            raise EmailAlreadyExistsError()

        password_hash = await asyncio.to_thread(hash_password, payload.password)    # CPU-bound
        if isinstance(payload, AdminUserCreate):
            is_active, is_superuser = payload.is_active, payload.is_superuser
        else:
            is_active, is_superuser = True, False
        user = User(
            email=payload.email,
            password_hash=password_hash,
            full_name=payload.full_name,
            is_active=is_active,
            is_superuser=is_superuser,
        )
        try:
            await self._repo.add(user)
            await self._session.commit()
        except IntegrityError as exc:
            # Los a race with a concurrent registration of the same email.
            await self._session.rollback()
            raise EmailAlreadyExistsError() from exc

        created = UserRead.model_validate(user)
        await self._enqueue_welcome_email(created.id)   # after commit, never before
        return created

    async def update_profile(self, user_id: UUID, payload: UserUpdateMe) -> UserRead:
        return await self._apply_changes(user_id, payload.model_dump(exclude_unset=True))

    async def admin_update_user(
        self, user_id: UUID, payload: UserAdminUpdate, *, actor_id: UUID
    ) -> UserRead:
        changes = payload.model_dump(exclude_unset=True)
        if user_id == actor_id and (
            changes.get("is_active") is False or changes.get("is_superuser") is False
        ):
            raise SelfModificationError("You cannot deactivate or demote yourself")
        return await self._apply_changes(user_id, changes)

    async def delete_user(self, user_id: UUID, *, actor_id: UUID) -> None:
        if user_id == actor_id:
            raise SelfModificationError("You cannot delete yourself")
        user = await self._get_or_raise(user_id)
        await self._repo.delete(user)   # refresh tokens are removed by on DELETE CASCADE
        await self._session.commit()
        await self._cache.delete(self._key(user_id))

    async def set_password(self, user_id: UUID, new_password: str) -> None:
        """Replace the password. COMMITS the current session (see AuthService.change_password)."""
        password_hash = await asyncio.to_thread(hash_password, new_password)
        await self._apply_changes(user_id, {"password_hash": password_hash})