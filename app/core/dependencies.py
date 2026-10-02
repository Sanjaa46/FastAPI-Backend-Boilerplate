from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.redis import get_redis_cache

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisCacheDep = Annotated[Redis, Depends(get_redis_cache)]