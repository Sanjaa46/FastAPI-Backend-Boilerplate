from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import Base

ModelT = TypeVar("ModelT", bound=Base)

class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, id_: UUID) -> ModelT | None:
        return await self._session.get(self.model, id_)

    async def add(self, entity: ModelT) -> ModelT:
        self._session.add(entity)
        await self._session.flush() # assigns PK/defaults, still inside the transaction
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self._session.delete(entity)
        await self._session.flush()