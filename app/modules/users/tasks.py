import uuid
from typing import Annotated

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqDepends

from app.core.database import get_session
from app.modules.users.repository import UserRepository
from app.worker.broker import broker

log = structlog.get_logger()


@broker.task(task_name="users.send_welcome_email", retry_on_error=True, max_retries=3)
async def send_welcome_email(
    user_id: str,   # primitives only: payloads are serialized (UUID is passed as a string)
    session: Annotated[AsyncSession, TaskiqDepends(get_session)],
) -> None:
    """Send the welcome email. Idempotent: running twice only sends a duplicate email.

    This is the integration point for a mail provider; it currently logs instead of sending email
    because the boilerplate deliberately ships no email provider.
    """
    user = await UserRepository(session).get(uuid.UUID(user_id))
    if user is None or not user.is_active:
        log.info("welcome_email_skipped", user_id=user_id)
        return
    log.info("welcome_email_sent", user_id=user_id, to=user.email)