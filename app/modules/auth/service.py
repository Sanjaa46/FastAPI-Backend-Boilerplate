import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import InvalidTokenError
from app.core.security import (
    create_token,
    decode_token,
    verify_dummy_password,
    verify_password,
)
from app.modules.auth.exceptions import (
    InactiveUserError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    RegisterationDisabledError,
)
from app.modules.auth.models import RefreshToken
from app.modules.auth.repository import RefreshTokenRepository
from app.modules.auth.schemas import TokenPair
from app.modules.users.exceptions import UserNotFoundError
from app.modules.users.schemas import UserCreate, UserRead
from app.modules.users.service import UserService

log = structlog.get_logger()


class AuthService:
    """Owns the transaction for every auth use case.

    `session` is the SAME request-scoped session the UserService uses, so one commit()
    persists the work of both services (see change_password).
    """

    def __init__(
        self,
        users: UserService,
        tokens: RefreshTokenRepository,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        self._users = users
        self._tokens = tokens
        self._session = session
        self._settings = settings

    # ------------------------------- helpers
    async def _issue_tokens(self, user_id: uuid.UUID, family_id: uuid.UUID) -> TokenPair:
        """Create an access+refresh pair and persist the refresh row. Does NOT commit."""
        access_ttl = timedelta(minutes=self._settings.access_token_ttl_minutes)
        access, _ = create_token(
            subject=user_id,
            kind="access",
            expires_delta=access_ttl,
            settings=self._settings,
        )
        refresh_id = uuid.uuid4()
        refresh, claims = create_token(
            subject=user_id,
            kind="refresh",
            expires_delta=timedelta(days=self._settings.refresh_token_ttl_days),
            settings=self._settings,
            jti=refresh_id,
        )
        await self._tokens.add(
            RefreshToken(
                id=refresh_id,
                user_id=user_id,
                family_id=family_id,
                expires_at=claims.expires_at,
            )
        )
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(access_ttl.total_seconds()),
        )

    # --------------------------------------------- use cases

    async def register(self, payload: UserCreate) -> UserRead:
        if not self._settings.registration_enabled:
            raise RegisterationDisabledError()
        return await self._users.create_user(payload)

    async def login(self, email: str, password: str) -> TokenPair:
        credentials = await self._users.get_credentials_by_email(email)
        if credentials is None:
            # Same CPU cost as a real check => timing does not reveal registered emails.
            await asyncio.to_thread(verify_dummy_password, password)
            raise InvalidCredentialsError()
        if not await asyncio.to_thread(verify_password, password, credentials.password_hash):
            raise InvalidCredentialsError()
        if not credentials.is_active:
            raise InactiveUserError()

        pair = await self._issue_tokens(credentials.id, family_id=uuid.uuid4())
        await self._session.commit()
        return pair

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Rotate: the presented token is consumed and a new pair in the same family is issued."""
        claims = decode_token(refresh_token, self._settings, expected_type="refresh")
        now = datetime.now(UTC)

        row = await self._tokens.get_for_update(claims.jti)
        if (
            row is None
            or row.user_id != claims.subject
            or row.revoked_at is not None
            or row.expires_at <= now
        ):
            raise InvalidTokenError()

        if row.used_at is not None:
            # An already-rotated token came back: either a replay or a stolen copy.
            # Revoke the whole family so neither party keeps a working session.
            await self._tokens.revoke_family(row.family_id, now)
            await self._session.commit()
            log.warning(
                "refresh_token_reuse_detected",
                user_id=str(row.user_id),
                family_id=str(row.family_id),
            )
            raise InvalidTokenError()

        try:
            user = await self._users.get_user(row.user_id)
        except UserNotFoundError as exc:
            raise InvalidTokenError() from exc
        if not user.is_active:
            raise InactiveUserError()

        row.used_at = now
        pair = await self._issue_tokens(row.user_id, row.family_id)
        await self._session.commit()
        return pair

    async def logout(self, refresh_token: str) -> None:
        """Reovoke the session (token family) behind this refresh token. Always idempotent."""
        try:
            claims = decode_token(refresh_token, self._settings, expected_type="refresh")
        except InvalidTokenError:
            return  # nothing to revoke; do not leak wheter the token was valid
        row = await self._tokens.get(claims.jti)
        if row is None:
            return
        await self._tokens.revoke_family(row.family_id, datetime.now(UTC))
        await self._session.commit()

    async def logout_all(self, user_id: uuid.UUID) -> None:
        """Revoke every session of the user."""
        await self._tokens.revoke_all_for_user(user_id, datetime.now(UTC))
        await self._session.commit()

    async def change_password(
        self, user_id: uuid.UUID, current_password: str, new_password: str
    ) -> TokenPair:
        """Change the password, revoke ALL sessions, and start a fresh session for this device."""
        credentials = await self._users.get_credentials(user_id)
        if credentials is None:
            raise InvalidTokenError()
        if not await asyncio.to_thread(
            verify_password, current_password, credentials.password_hash
        ):
            raise InvalidCurrentPasswordError()

        await self._tokens.revoke_all_for_user(user_id, datetime.now(UTC))  # flushed only
        # set_password() commits the shared session: revocation + new hash are ONE transaction.
        await self._users.set_password(user_id, new_password)

        pair = await self._issue_tokens(user_id, family_id=uuid.uuid4())
        await self._session.commit()
        return pair
