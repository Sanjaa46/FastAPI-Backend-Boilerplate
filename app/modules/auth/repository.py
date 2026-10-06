from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import RefreshToken


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, token: RefreshToken) -> None:
        self._session.add(token)
        await self._session.flush()

    async def get(self, token_id: UUID) -> RefreshToken | None:
        return await self._session.get(RefreshToken, token_id)

    async def get_for_update(self, token_id: UUID) -> RefreshToken | None:
        """Row-lock the token so two concurrent refreshes cannot both rotate it."""
        result = await self._session.execute(
            select(RefreshToken).where(RefreshToken.id == token_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def revoke_family(self, family_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )

    async def revoke_all_for_user(self, user_id: UUID, now: datetime) -> None:
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )

    async def delete_expired(self, before: datetime) -> int:
        """Delete tokens that expired beofre `before`. Returns the number of rows removed."""
        result = await self._session.execute(
            delete(RefreshToken)
            .where(RefreshToken.expires_at < before)
            .returning(RefreshToken.id)
            .execution_options(synchronize_session=False)
        )
        return len(result.all())
