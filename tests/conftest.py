"""Test infrastructure.

* Real PostgreSQL + Redis (no mocks for data). The test database is created on demand and
  rebuilt from the models once per run.
* Every test runs inside a transaction that is rolled back (SAVEPOINT mode), so the
  services' own `commit()` calls never leak between tests.
* The environment is configured BEFORE the app is imported: settings are cached at import.

Defaults match docker-compose (`docker compose up -d postgres redis-cache redis-broker`).
Override with TEST_DATABASE_URL / TEST_REDIS_URL.
"""

import os

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DEBUG": "false",
        "LOG_LEVEL": "WARNING",
        "LOG_JSON": "false",
        "DATABASE_URL": os.environ.get(
            "TEST_DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/app_test"
        ),
        "REDIS_CACHE_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379") + "/15",
        "REDIS_BROKER_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6379") + "/14",
        "JWT_SECRET_KEY": "test-only-secret-test-only-secret-0123456789",
        "REGISTRATION_ENABLED": "true",
        # Off, whatever the developer's .env says; rate-limit tests switch it on explicitly.
        "RATE_LIMIT_ENABLED": "false",
    }
)

from collections.abc import AsyncIterator, Awaitable, Callable
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.models
from app.core.config import Settings, get_settings
from app.core.database import Base, get_session
from app.core.redis import create_redis
from app.core.security import hash_password
from app.main import app
from app.modules.users.models import User

# One known password for every fixture user (hashed once: argon2 is deliberately slow).
PASSWORD = "correct-horse-battery-staple"

UserFactory = Callable[..., Awaitable[User]]
LoginFn = Callable[[str, str], Awaitable[dict[str, str]]]


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Create the test database if needed and rebuild its schema from the models."""
    url = make_url(str(settings.database_url))
    assert url.database is not None and url.database.endswith("_test"), (
        "refusing to run destructive test setup against a non-test database"
    )

    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = await conn.scalar(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        )
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()

    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
async def redis_cache(settings: Settings) -> AsyncIterator[Redis]:
    client = create_redis(str(settings.redis_cache_url))
    yield client
    await client.aclose()


@pytest.fixture(scope="session")
async def redis_broker(settings: Settings) -> AsyncIterator[Redis]:
    client = create_redis(str(settings.redis_broker_url))
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _flush_cache(redis_cache: Redis) -> None:
    """Start every test with an empty cache."""
    await redis_cache.flushdb()


@pytest.fixture(autouse=True)
def welcome_email_kiq(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the welcome-email enqueue everywhere; tests assert on this mock."""
    kiq = AsyncMock()
    stub = type("StubTask", (), {"kiq": kiq})()
    monkeypatch.setattr("app.modules.users.service.send_welcome_email", stub)
    return kiq


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Everything a test does is rolled back, including the services' own commit() calls."""
    async with engine.connect() as conn:
        outer = await conn.begin()
        async with AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as s:
            yield s
        await outer.rollback()


@pytest.fixture
async def client(
    session: AsyncSession, engine: AsyncEngine, redis_cache: Redis, redis_broker: Redis
) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the app with the rollback session injected.

    ASGITransport does not run the lifespan, so the resources it would create are set on
    app.state here.
    """

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override_session
    app.state.redis_cache = redis_cache
    app.state.redis_broker = redis_broker
    app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def make_user(session: AsyncSession, password_hash: str) -> UserFactory:
    """Insert a user directly (fast path; password is PASSWORD)."""
    counter = 0

    async def _make(
        email: str | None = None,
        *,
        is_active: bool = True,
        is_superuser: bool = False,
        full_name: str = "Test User",
    ) -> User:
        nonlocal counter
        counter += 1
        user = User(
            email=email or f"user{counter}@example.com",
            password_hash=password_hash,
            full_name=full_name,
            is_active=is_active,
            is_superuser=is_superuser,
        )
        session.add(user)
        await session.commit()
        return user

    return _make


@pytest.fixture
def login(client: AsyncClient) -> LoginFn:
    """Log in through the API and return the token pair JSON."""

    async def _login(email: str, password: str = PASSWORD) -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.text
        data: dict[str, str] = response.json()
        return data

    return _login


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    """Authorization header for a token pair returned by login()."""
    return {"Authorization": f"Bearer {tokens['access_token']}"}
