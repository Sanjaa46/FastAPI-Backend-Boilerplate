from collections.abc import Sequence

from sqlalchemy import func, select

from app.common.repository import BaseRepository
from app.modules.users.models import User


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def list_page(self, *, limit: int, offset: int) -> Sequence[User]:
        result = await self._session.execute(
            select(User).order_by(User.created_at.desc(), User.id).limit(limit).offset(offset)
        )
        return result.scalars().all()

    async def count(self) -> int:
        result = await self._session.execute(select(func.count()).select_from(User))
        return result.scalar_one()
