"""structlog configuration: JSON in non-local environments, console renderer locally.

Both structlog loggers and stdlib loggers (uvicorn, sqlalchemy, taskiq) are routed through
the same processors, so every log line has the same shape and carries the request ID.
"""

import logging
import sys

import structlog
from structlog.typing import Processor

from app.core.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging. Safe to call more than once."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,  # request_id, method, path
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Processor
    final: list[Processor]
    if settings.log_json:
        renderer = structlog.processors.JSONRenderer()
        final = [structlog.processors.format_exc_info, renderer]  # tracebacks as strings
    else:
        renderer = structlog.dev.ConsoleRenderer()
        final = [renderer]

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,  # applied to records from plain stdlib loggers
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *final],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # Uvicorn installs its own handlers; hand its records to the root handler instead.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # We emit our own structured access log (RequestContextMiddleware).
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
