from datetime import UTC, datetime
from typing import Annotated

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqDepends

from app.core.database import get_session
from app.modules.auth.repository import RefreshTokenRepository
from app.worker.broker import broker

log = structlog.get_logger()


@broker.task(
    task_name="auth.purge_expired_refresh_tokens",
    schedule=[{"cron": "0 * * * *"}],   # hourly; requires exactly ONE scheduler process
)
async def purge_expired_refresh_tokens(
    session: Annotated[AsyncSession, TaskiqDepends(get_session)],
) -> int:
    """Delete refresh tokens past their expiry (used/revoked ones are kept until then so
    reuse detection keeps working). Idempotent. Returns the number of deleted rows."""
    deleted = await RefreshTokenRepository(session).delete_expired(datetime.now(UTC))
    await session.commit()
    log.info("expired_refresh_tokens_purged", deleted=deleted)
    return deleted