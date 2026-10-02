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