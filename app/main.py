"""Application factory and lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.database import create_engine, create_session_factory
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import register_middleware
from app.core.redis import create_redis
from app.modules.health.router import router as health_router
from app.worker import tasks as _tasks  # noqa: F401  # registers tasks on the broker (API can .kiq)
from app.worker.broker import broker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create shared resources on startup, release them on shutdown.

    Taskiq runs this same lifespan inside the worker process, so the engine and Redis
    clients exist there too.
    """
    settings = get_settings()
    configure_logging(settings)

    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis_cache = create_redis(str(settings.redis_cache_url))
    app.state.redis_broker = create_redis(str(settings.redis_broker_url))  # /health/ready only
    if not broker.is_worker_process:  # API process only; the worker starts its own broker
        await broker.startup()
    try:
        yield
    finally:
        if not broker.is_worker_process:
            await broker.shutdown()
        await app.state.redis_broker.aclose()
        await app.state.redis_cache.aclose()
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    is_prod = settings.environment == "production"
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )
    register_middleware(app, settings)
    register_exception_handlers(app)
    app.include_router(health_router, prefix="/health", tags=["health"])
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
