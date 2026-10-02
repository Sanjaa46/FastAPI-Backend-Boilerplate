# FastAPI Backend Boilerplate — Build & Handover Guide

**Audience:** mid-level backend developer who will build, run, and extend this boilerplate.
**Targets:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x (async), PostgreSQL, Redis, uv, Alembic, Taskiq, Docker.
**Version policy:** library versions are deliberately **not pinned in this document**. `uv.lock` is the single source of truth. Anything marked **VERIFY** depends on a library API that changes between releases; check it against the installed version.

---

## 1. Scope and decisions

### 1.1 What "perfect" means here

"Perfect" has no end condition, so this boilerplate defines one: **a feature is in the core if every new project would need it on day one.** Everything else is listed in [§20 Deliberately excluded](#20-deliberately-excluded) or marked *optional (off by default)*. Do not add features to the core without a written reason in `docs/adr/`.

### 1.2 Terminology fix

**Alembic is not an ORM.** It is a migration tool. The ORM is **SQLAlchemy 2.x (async)**; Alembic generates migrations from SQLAlchemy models.

### 1.3 Stack decisions

| Concern | Choice | Why |
|---|---|---|
| Web framework | FastAPI + Uvicorn | Async-native, typed, built-in DI |
| Database | PostgreSQL via `asyncpg` | Required; fastest async Postgres driver |
| ORM | SQLAlchemy 2.x async (`Mapped[]` style) | Standard; typed; works with Alembic |
| Migrations | Alembic (async template) | Required |
| Cache / broker | Redis (`redis.asyncio`) | Required |
| Worker | **Taskiq** + `taskiq-redis` + `taskiq-fastapi` | Async-native; tasks can reuse FastAPI dependencies |
| Settings | `pydantic-settings` | Typed, validated env config |
| Auth | JWT (`PyJWT`) + `pwdlib[argon2]` | Maintained, minimal |
| Logging | `structlog` (JSON in non-local envs) | Structured, request-ID friendly |
| Packaging | **uv** (`uv.lock` committed) | Required; fast, reproducible |
| Lint / format | `ruff` | One tool for both |
| Types | `mypy --strict` | Catches DI/typing mistakes early |
| Tests | `pytest`, `pytest-asyncio`, `httpx` | Standard async stack |
| Containers | Docker multi-stage, Docker Compose | Required |

**Why Taskiq and not Celery:** Celery is sync-first; running async code in it needs workarounds. Taskiq is async-native and `taskiq-fastapi` lets a task use the same `Depends(...)` factories as an HTTP route, which is what makes the DI requirement hold in the worker too. Trade-off: smaller ecosystem and community than Celery.

### 1.4 Architectural decisions (read these first)

1. **Feature modules, not technical layers.** Code is grouped by business capability (`users/`, `auth/`), each with its own router/service/repository/models.
2. **Router → Service → Repository.** Routers do HTTP only. Services hold business logic and own the transaction. Repositories do SQL only.
3. **Services own commits.** Repositories `flush()`; the service calls `commit()` once per use case. The session dependency never auto-commits.
4. **No global singletons for stateful clients.** Engine, session factory, and Redis clients are created in the app `lifespan`, stored on `app.state`, and exposed only through dependencies.
5. **Two Redis instances, one image.** `redis-cache` (eviction allowed) and `redis-broker` (no eviction, persistence on). Reason: eviction policy is instance-wide; a cache with `allkeys-lru` sharing an instance with the task queue can evict queued tasks.
6. **Cache explicitly in services, not with decorators.** Decorators hide the DI graph and make invalidation hard to reason about.
7. **Cache failure never fails a request.** Redis errors in the cache layer are logged and fall through to the database.
8. **UTC everywhere.** All timestamps are `timestamptz`; all datetimes are timezone-aware.
9. **Migrations are a separate deploy step**, never run implicitly on app start (race condition with multiple replicas).

---

## 2. Prerequisites and quick start

### 2.1 Prerequisites

- Docker Engine 24+ and Docker Compose v2
- [uv](https://docs.astral.sh/uv/) installed locally
- Git, `make` (on Windows `make` is not installed by default: use WSL, install GNU Make, or run the underlying `uv run ...` commands from the Makefile directly)
- Python 3.12 (uv installs it for you if missing)

### 2.2 Quick start (using an already-built repo)

```bash
git clone <repo-url> && cd <repo>
cp .env.example .env              # then edit secrets
uv sync                           # creates .venv from uv.lock (includes dev group)
docker compose up -d postgres redis-cache redis-broker
uv run alembic upgrade head       # apply migrations
uv run uvicorn app.main:app --reload   # API on http://localhost:8000
# in a second terminal:
uv run taskiq worker app.worker.broker:broker app.worker.tasks
```

Or everything in containers:

```bash
docker compose up --build         # postgres, redis x2, migrate, api, worker, scheduler
```

- OpenAPI docs: `http://localhost:8000/docs` (disabled when `ENVIRONMENT=production`)
- Liveness: `GET /health/live` — Readiness: `GET /health/ready`

> When running the API **on the host** (not in Docker), point `.env` at `localhost` instead of the Compose service names (`postgres`, `redis-cache`, `redis-broker`).

### 2.3 Daily commands

| Task | Command |
|---|---|
| Add runtime dependency | `uv add <pkg>` |
| Add dev dependency | `uv add --dev <pkg>` |
| Upgrade all | `uv lock --upgrade && uv sync` |
| Run anything in the env | `uv run <cmd>` |
| New migration | `make revision m="add orders table"` |
| Apply migrations | `make migrate` |
| Lint + format + types | `make check` |
| Tests | `make test` |

Commit `uv.lock` and `.python-version`. Never `pip install` into the project.

---

## 3. Building the boilerplate from scratch (build order)

Follow in order; each phase ends with something runnable.

**Phase 1 — Skeleton**
```bash
mkdir fastapi-boilerplate && cd fastapi-boilerplate && git init
uv init --app --python 3.12 --no-readme   # plain app. Do NOT pass --package or --lib.
# Delete the generated main.py. Then open pyproject.toml and make sure it has NO [build-system]
# and NO [project.scripts], and that it contains:   [tool.uv]  package = false
# (Otherwise `uv add` tries to build this project as a package and fails with
#  "Expected a Python module at: src\<name>\__init__.py".)
# Shell note: the backslash line continuation below is bash. In PowerShell use a backtick (`)
# or put the whole command on one line.
uv add fastapi "uvicorn[standard]" "pydantic[email]" pydantic-settings \
       "sqlalchemy[asyncio]" asyncpg alembic redis \
       taskiq taskiq-redis taskiq-fastapi \
       structlog pyjwt "pwdlib[argon2]"
uv add --dev pytest pytest-asyncio pytest-cov httpx ruff mypy pre-commit
```
Create the folder tree from [§4](#4-project-structure). Add `GET /health/live`. Run it.

**Phase 2 — Config and logging.** `core/config.py`, `core/logging.py`, request-ID middleware ([§6](#6-configuration), [§11](#11-api-conventions)).

**Phase 3 — Database.** Engine, session factory, `Base`, mixins, `get_session` ([§7](#7-database-sqlalchemy-async)).

**Phase 4 — Migrations.** `alembic init -t async migrations`, wire `env.py`, generate the first migration ([§8](#8-migrations-alembic)).

**Phase 5 — Redis + cache.** Redis clients in lifespan, `CacheService`, readiness endpoint ([§9](#9-redis-and-caching)).

**Phase 6 — Reference modules.** `users` (CRUD, cached read) and `auth` (register/login/refresh). These are the **templates developers copy** ([§17](#17-adding-a-new-module)).

**Phase 7 — Worker.** Broker, one real task (`send_welcome_email` stub), scheduler with one cron task ([§10](#10-background-worker-taskiq)).

**Phase 8 — Tests.** Fixtures first, then tests for `users` and `auth` ([§13](#13-testing)).

**Phase 9 — Tooling.** `ruff`, `mypy`, `pre-commit`, `Makefile` ([§14](#14-code-quality-and-tooling)).

**Phase 10 — Docker + CI.** Dockerfile, both Compose files, GitHub Actions ([§15](#15-docker-and-deployment), [§16](#16-ci)).

**Phase 11 — Acceptance.** Run the checklist in [§19](#19-acceptance-checklist). Do not hand over until every box is ticked.

---

## 4. Project structure

```
.
├── app/
│   ├── main.py                    # create_app(), lifespan, `app = create_app()`
│   ├── models.py                  # imports every module's models (Alembic discovery)
│   ├── api/
│   │   └── v1/
│   │       └── router.py          # aggregates module routers under /api/v1
│   ├── core/                      # cross-cutting infrastructure (no business logic)
│   │   ├── config.py              # Settings + get_settings()
│   │   ├── database.py            # Base, engine/session factories, get_session
│   │   ├── redis.py               # Redis client factories, get_redis_cache
│   │   ├── cache.py               # CacheService
│   │   ├── security.py            # password hashing, JWT encode/decode
│   │   ├── logging.py             # structlog setup
│   │   ├── middleware.py          # request ID, access log, security headers
│   │   ├── exceptions.py          # AppError hierarchy + exception handlers
│   │   └── dependencies.py        # shared Annotated aliases (SessionDep, ...)
│   ├── common/                    # reusable building blocks
│   │   ├── schemas.py             # Page[T], ErrorResponse, base schemas
│   │   ├── models.py              # TimestampMixin, UUIDPrimaryKeyMixin
│   │   └── repository.py          # generic BaseRepository[ModelT]
│   ├── modules/
│   │   ├── health/
│   │   │   └── router.py
│   │   ├── auth/
│   │   │   ├── router.py  schemas.py  service.py  dependencies.py  exceptions.py
│   │   └── users/
│   │       ├── router.py       # HTTP only
│   │       ├── schemas.py      # Pydantic request/response models
│   │       ├── models.py       # SQLAlchemy models
│   │       ├── repository.py   # SQL only
│   │       ├── service.py      # business logic + transaction + cache
│   │       ├── dependencies.py # get_user_repository / get_user_service
│   │       ├── tasks.py        # Taskiq tasks owned by this module
│   │       └── exceptions.py   # UserNotFoundError, ...
│   └── worker/
│       ├── broker.py           # broker, result backend, middlewares
│       ├── scheduler.py        # TaskiqScheduler
│       └── tasks.py            # imports every module's tasks.py (task discovery)
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
├── tests/
│   ├── conftest.py
│   ├── unit/                   # services with fakes, no I/O
│   ├── integration/            # real Postgres + Redis
│   └── modules/                # per-module API tests
├── docker/
│   └── postgres/init-test-db.sql
├── docs/adr/                   # architecture decision records
├── .github/workflows/ci.yml
├── .env.example
├── .dockerignore
├── .gitignore
├── .pre-commit-config.yaml
├── .python-version
├── alembic.ini
├── docker-compose.yml          # development
├── docker-compose.prod.yml     # production
├── Dockerfile
├── Makefile
├── pyproject.toml
└── uv.lock
```

### Import rules (enforce in code review)

| From | May import |
|---|---|
| `modules/<x>/router.py` | its own `schemas`, `service` (via dependency), `core.dependencies` |
| `modules/<x>/service.py` | its own `repository`, `schemas`, `exceptions`; `core.cache`; **other modules' services** |
| `modules/<x>/repository.py` | its own `models`, `common.repository` |
| `core/*` | other `core/*`, `common/*` — **never** `modules/*` |
| `common/*` | `core/*` only |

A module **never imports another module's repository or models directly.** If two modules need each other, extract the shared piece to `common/` or communicate through a task.

---

## 5. Layering and dependency injection

### 5.1 Responsibilities

| Layer | Does | Must not |
|---|---|---|
| Router | Parse request, call one service method, shape response, set status code | Touch the session, write SQL, contain business rules |
| Service | Business rules, orchestration, `commit()`, cache read/invalidate, enqueue tasks | Know about `Request`/`Response`, return ORM objects to routers |
| Repository | Queries and `add/flush/delete` | Commit, call other repositories, raise HTTP errors |
| Schema | Validation and serialization | Contain DB logic |

Services return **Pydantic schemas** (not ORM instances) so cached and uncached paths return the same type and lazy-loading errors cannot leak into routers.

### 5.2 The DI chain

```
get_session(request)          ─┐
get_redis_cache(request)      ─┼─►  get_user_repository(session)
                               │    get_user_service(repo, cache, session)
                               └─►  router: UserServiceDep
```

```python
# app/core/dependencies.py
"""Shared, typed dependency aliases. Routers import these instead of writing Depends() inline."""
from typing import Annotated

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.redis import get_redis_cache

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisCacheDep = Annotated[Redis, Depends(get_redis_cache)]
```

```python
# app/modules/users/dependencies.py
"""Wiring for the users module. This is the only place that knows how to construct UserService."""
from typing import Annotated

from fastapi import Depends

from app.core.cache import CacheService
from app.core.config import Settings, get_settings
from app.core.dependencies import RedisCacheDep, SessionDep
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService


def get_user_repository(session: SessionDep) -> UserRepository:
    return UserRepository(session)


def get_user_service(
    session: SessionDep,
    redis: RedisCacheDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> UserService:
    cache = CacheService(
        redis,
        prefix=f"{settings.cache_prefix}:users",
        default_ttl=settings.cache_default_ttl_seconds,
    )
    return UserService(UserRepository(session), cache, session)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
```

```python
# app/modules/users/router.py
"""HTTP surface for users. No SQL, no business logic."""
from uuid import UUID

from fastapi import APIRouter, status

from app.modules.users.dependencies import UserServiceDep
from app.modules.users.schemas import UserCreate, UserRead

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, service: UserServiceDep) -> UserRead:
    return await service.create_user(payload)


@router.get("/{user_id}", response_model=UserRead)
async def get_user(user_id: UUID, service: UserServiceDep) -> UserRead:
    return await service.get_user(user_id)
```

**Rules**
- Always use `Annotated[T, Depends(...)]`, never `x: T = Depends(...)` (ruff `B008`, and it keeps signatures clean).
- Dependencies are plain functions/generators; **no hidden global state**.
- Tests replace behavior with `app.dependency_overrides[get_user_service] = ...`.

---

## 6. Configuration

All config comes from environment variables, validated at startup. The app **refuses to boot** with invalid or unsafe configuration.

```python
# app/core/config.py
"""Typed application settings. Single source of truth for all configuration."""
from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- App ---
    app_name: str = "fastapi-boilerplate"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = []          # env: CORS_ORIGINS='["https://app.example.com"]'

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True                 # false => human-readable console output

    # --- PostgreSQL ---
    database_url: PostgresDsn             # postgresql+asyncpg://user:pass@host:5432/db
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800

    # --- Redis (two instances, see ADR) ---
    redis_cache_url: RedisDsn
    redis_broker_url: RedisDsn

    # --- Cache ---
    cache_prefix: str = "app"
    cache_default_ttl_seconds: int = 300

    # --- Auth ---
    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7

    @model_validator(mode="after")
    def _reject_unsafe_production(self) -> "Settings":
        """Fail fast: never boot production with a placeholder secret or debug on."""
        if self.environment == "production":
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            if self.jwt_secret_key.get_secret_value() in {"", "change-me"}:
                raise ValueError("JWT_SECRET_KEY must be set to a strong secret in production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Override in tests via dependency_overrides."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
```

### `.env.example`

```dotenv
ENVIRONMENT=local
DEBUG=true
LOG_LEVEL=DEBUG
LOG_JSON=false
CORS_ORIGINS=["http://localhost:3000"]

DATABASE_URL=postgresql+asyncpg://app:app@postgres:5432/app
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=10

REDIS_CACHE_URL=redis://redis-cache:6379/0
REDIS_BROKER_URL=redis://redis-broker:6379/0
CACHE_PREFIX=app
CACHE_DEFAULT_TTL_SECONDS=300

JWT_SECRET_KEY=change-me
ACCESS_TOKEN_TTL_MINUTES=15
REFRESH_TOKEN_TTL_DAYS=7

# Used by docker-compose postgres service
POSTGRES_USER=app
POSTGRES_PASSWORD=app
POSTGRES_DB=app
```

**Rules:** `.env` is git-ignored; `.env.example` lists *every* variable; secrets in production come from the platform's secret store, never from the image.

---

## 7. Database (SQLAlchemy async)

### 7.1 Base, mixins, session

```python
# app/core/database.py
"""Async SQLAlchemy setup: Base, engine/session factories, request-scoped session."""
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings

# Deterministic constraint names => stable, reviewable Alembic migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,  # detect dead connections after DB restarts
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: attributes stay readable after commit (no implicit lazy
    # reload, which raises MissingGreenlet in async code).
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Does NOT commit; services own the transaction."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session  # closing the context rolls back anything uncommitted
```

> For the worker: if you reuse `get_session` in a Taskiq task, change the signature to `request: Annotated[Request, TaskiqDepends()]` (see [§10.4](#104-using-dependencies-in-tasks)).

```python
# app/common/models.py
"""Mixins every table gets by default."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

### 7.2 Repository pattern

```python
# app/common/repository.py
"""Generic repository. Repositories flush; they never commit."""
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
        await self._session.flush()  # assigns PK/defaults, still inside the transaction
        return entity
```

### 7.3 Rules

- **No lazy loading.** Load relations explicitly (`selectinload`, `joinedload`). Lazy loads in async raise `MissingGreenlet`.
- **Never share one `AsyncSession` across concurrent coroutines** (`asyncio.gather`). One session = one task.
- **Pool sizing:** `(pool_size + max_overflow) × uvicorn_workers × replicas` must stay below Postgres `max_connections` minus headroom (migrations, admin, worker). If it can't, add PgBouncer — do not raise `max_connections` blindly.
- Use `select(...)` 2.0-style queries. No legacy `Query` API.
- Blocking/CPU-bound work (password hashing, large JSON, PDF generation) goes through `await asyncio.to_thread(...)`. **A sync call inside `async def` blocks the whole event loop.**

---

## 8. Migrations (Alembic)

### 8.1 Setup

```bash
uv run alembic init -t async migrations
```

Edit `alembic.ini`: leave `sqlalchemy.url` empty, and set a sortable filename:

```ini
file_template = %%(year)d%%(month).2d%%(day).2d_%%(hour).2d%%(minute).2d_%%(rev)s_%%(slug)s
```

```python
# migrations/env.py
"""Alembic environment (async). Reads the DB URL from app settings, not alembic.ini."""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401  # registers every module's models on Base.metadata
from app.core.config import get_settings
from app.core.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# '%' must be escaped for configparser interpolation (passwords may contain it).
config.set_main_option("sqlalchemy.url", str(get_settings().database_url).replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (`alembic upgrade head --sql`)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,            # detect column type changes
        compare_server_default=True,  # detect server_default changes
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # one-shot process; no pooling needed
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

```python
# app/models.py
"""Import every module's models here so Alembic autogenerate can see them."""
from app.modules.users.models import User  # noqa: F401
```

### 8.2 Workflow

```bash
make revision m="add orders table"   # = alembic revision --autogenerate -m "..."
# 1. OPEN the generated file and review it. Autogenerate is a draft, not an oracle.
make migrate                         # = alembic upgrade head
uv run alembic downgrade -1          # test the downgrade path locally
uv run alembic check                 # fails if models and migrations disagree
```

### 8.3 Rules

1. **Always review autogenerated migrations.** Autogenerate misses renames (it emits drop+add → data loss), some constraints, and server defaults.
2. **Never edit a migration that has been merged/applied.** Write a new one.
3. **One head.** If `alembic heads` shows two, merge before the PR is approved.
4. **Backward-compatible deploys:** add column (nullable/default) → deploy code → backfill → then enforce `NOT NULL`. Never drop/rename a column in the same release that stops using it.
5. **Large tables:** create indexes with `postgresql_concurrently=True` inside an `autocommit_block()`.
6. Migrations run in a **dedicated one-shot container/job** ([§15](#15-docker-and-deployment)), never on API startup.
7. Data migrations are explicit, idempotent, and separate from schema migrations.

---

## 9. Redis and caching

### 9.1 Clients

```python
# app/core/redis.py
"""Redis client factories. One pool per Redis instance, created in lifespan."""
from fastapi import Request
from redis.asyncio import Redis


def create_redis(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True, health_check_interval=30)


async def get_redis_cache(request: Request) -> Redis:
    """Cache-only Redis. The broker Redis is never exposed to request handlers."""
    redis: Redis = request.app.state.redis_cache
    return redis
```

### 9.2 CacheService (cache-aside)

```python
# app/core/cache.py
"""Cache-aside helper. Values are Pydantic models serialized as JSON.

Contract: a Redis failure NEVER fails the request. We log and fall through to the loader.
"""
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import structlog
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger()
ModelT = TypeVar("ModelT", bound=BaseModel)


class CacheService:
    def __init__(self, redis: Redis, *, prefix: str, default_ttl: int) -> None:
        self._redis = redis
        self._prefix = prefix
        self._default_ttl = default_ttl

    def key(self, *parts: str | int) -> str:
        """Build a namespaced key, e.g. app:users:v1:<id>. Bump `v1` when the schema changes."""
        return ":".join((self._prefix, *(str(p) for p in parts)))

    async def get(self, key: str, model: type[ModelT]) -> ModelT | None:
        try:
            raw = await self._redis.get(key)
        except RedisError:
            log.warning("cache_get_failed", key=key, exc_info=True)
            return None
        return None if raw is None else model.model_validate_json(raw)

    async def set(self, key: str, value: BaseModel, ttl: int | None = None) -> None:
        base_ttl = ttl or self._default_ttl
        # Jitter (+0-10%) spreads expirations so hot keys don't all expire together.
        expires = base_ttl + random.randint(0, max(1, base_ttl // 10))  # noqa: S311
        try:
            await self._redis.set(key, value.model_dump_json(), ex=expires)
        except RedisError:
            log.warning("cache_set_failed", key=key, exc_info=True)

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self._redis.delete(*keys)
        except RedisError:
            log.warning("cache_delete_failed", keys=keys, exc_info=True)

    async def get_or_load(
        self,
        key: str,
        model: type[ModelT],
        loader: Callable[[], Awaitable[ModelT]],
        ttl: int | None = None,
    ) -> ModelT:
        """Return the cached value, or call `loader`, cache the result, and return it."""
        cached = await self.get(key, model)
        if cached is not None:
            return cached
        value = await loader()
        await self.set(key, value, ttl)
        return value
```

### 9.3 Using it in a service

```python
# app/modules/users/service.py  (excerpt)
class UserService:
    def __init__(self, repo: UserRepository, cache: CacheService, session: AsyncSession) -> None:
        self._repo = repo
        self._cache = cache
        self._session = session

    def _key(self, user_id: UUID) -> str:
        return self._cache.key("v1", user_id)

    async def get_user(self, user_id: UUID) -> UserRead:
        async def load() -> UserRead:
            user = await self._repo.get(user_id)
            if user is None:
                raise UserNotFoundError(user_id)  # never cached
            return UserRead.model_validate(user)

        return await self._cache.get_or_load(self._key(user_id), UserRead, load)

    async def rename_user(self, user_id: UUID, name: str) -> UserRead:
        user = await self._repo.get(user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        user.name = name
        await self._session.commit()                 # 1) commit first
        await self._cache.delete(self._key(user_id)) # 2) then invalidate
        return UserRead.model_validate(user)
```

### 9.4 Caching rules

| Rule | Reason |
|---|---|
| Every key has a TTL | Memory is bounded; stale data self-heals if an invalidation is missed |
| Key format `<prefix>:<module>:v<N>:<id>` | Namespacing + safe schema changes (bump `vN`) |
| Invalidate **after commit** | Invalidating before commit lets a concurrent reader re-cache old data |
| Never `KEYS` / pattern-delete in production | O(N), blocks Redis. Use explicit keys or versioned namespaces |
| Cache DTOs (Pydantic), not ORM objects | ORM objects are session-bound and not serializable |
| Don't cache per-user secrets or anything authorization-dependent without the user in the key | Data leak between users |
| Don't cache errors/"not found" by default | Keeps behavior simple; add short negative caching only with evidence |

Use **HTTP caching** (`ETag`/`Cache-Control`) for public GET endpoints where clients/CDNs can help. That is separate from the Redis layer.

*Optional (off by default):* single-flight lock (`SET key NX PX`) to prevent cache stampedes on very hot keys. Add only when a measured hot key justifies it.

### 9.5 Redis instance configuration

| Instance | Purpose | Key settings |
|---|---|---|
| `redis-cache` | Cache | `--maxmemory 256mb --maxmemory-policy allkeys-lru`, persistence off |
| `redis-broker` | Task queue + results | `--appendonly yes --maxmemory-policy noeviction` |

---

## 10. Background worker (Taskiq)

### 10.1 Processes

| Process | Command | Replicas |
|---|---|---|
| API | `uvicorn app.main:app ...` | N |
| Worker | `taskiq worker app.worker.broker:broker app.worker.tasks` | N |
| Scheduler | `taskiq scheduler app.worker.scheduler:scheduler app.worker.tasks` | **exactly 1** (more = duplicated cron runs) |

All three use the **same image**; only the command differs.

### 10.2 Broker

```python
# app/worker/broker.py
"""Taskiq broker wired to FastAPI so tasks can reuse FastAPI dependencies."""
import taskiq_fastapi
from taskiq import SimpleRetryMiddleware
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from app.core.config import get_settings

_settings = get_settings()

# Streams give acknowledgements => at-least-once delivery. (VERIFY broker class names
# against the installed taskiq-redis version.)
broker = RedisStreamBroker(url=str(_settings.redis_broker_url)).with_result_backend(
    RedisAsyncResultBackend(redis_url=str(_settings.redis_broker_url), result_ex_time=3600)
)
broker.add_middlewares(SimpleRetryMiddleware(default_retry_count=3))

# Lazy string path: the worker imports the FastAPI app (and runs its lifespan) itself.
taskiq_fastapi.init(broker, "app.main:app")
```

Because the worker runs the **FastAPI lifespan**, the engine, session factory and Redis clients exist in the worker exactly as they do in the API. The `if not broker.is_worker_process` guard in `lifespan` prevents the broker from being started twice.

```python
# app/main.py
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
from app.worker.broker import broker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)

    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.redis_cache = create_redis(str(settings.redis_cache_url))
    if not broker.is_worker_process:  # API process only; the worker starts its own broker
        await broker.startup()
    try:
        yield
    finally:
        if not broker.is_worker_process:
            await broker.shutdown()
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
```

### 10.3 Defining and enqueuing tasks

```python
# app/modules/users/tasks.py
"""Background tasks owned by the users module."""
from typing import Annotated
from uuid import UUID

from taskiq import TaskiqDepends

from app.modules.users.dependencies import get_user_service
from app.modules.users.service import UserService
from app.worker.broker import broker


@broker.task(task_name="users.send_welcome_email", retry_on_error=True, max_retries=3)
async def send_welcome_email(
    user_id: UUID,  # pass IDs/primitives, never ORM objects
    service: Annotated[UserService, TaskiqDepends(get_user_service)],
) -> None:
    await service.send_welcome_email(user_id)  # MUST be idempotent (see rules)
```

```python
# app/worker/tasks.py
"""Task discovery: the worker CLI imports this module; import every module's tasks here."""
from app.modules.users import tasks as users_tasks  # noqa: F401
```

Enqueue from a service **after commit**:

```python
await self._session.commit()
await send_welcome_email.kiq(user.id)
```

### 10.4 Using dependencies in tasks

`taskiq-fastapi` lets tasks use the same dependency functions as routes. One catch: FastAPI forbids `Depends` for `Request`, so any dependency that takes `request: Request` must mark it for Taskiq:

```python
from typing import Annotated
from fastapi import Request
from taskiq import TaskiqDepends

async def get_session(request: Annotated[Request, TaskiqDepends()]) -> AsyncIterator[AsyncSession]:
    ...
```

Apply this to `get_session` and `get_redis_cache`. In HTTP requests it behaves identically. **VERIFY** with a smoke test (a task that opens a session and runs `SELECT 1`) — this is a required test in §13.

### 10.5 Scheduler

```python
# app/worker/scheduler.py
"""Cron/interval scheduling. Run exactly ONE scheduler process."""
from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from app.worker.broker import broker

scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])
```

```python
@broker.task(task_name="maintenance.purge_expired_tokens", schedule=[{"cron": "0 * * * *"}])
async def purge_expired_tokens() -> None: ...
```

### 10.6 Task rules

1. **Idempotent.** Delivery is at-least-once; the same task may run twice. Use natural idempotency keys or a "done" marker.
2. **Pass IDs, reload inside the task.** Payloads are serialized; ORM objects and sessions are not.
3. **Enqueue after commit**, or the worker may read rows that don't exist yet.
4. **Explicit `task_name`.** Renaming a function must not break queued messages.
5. **Timeouts and retries are explicit.** No unbounded retries; permanent failures go to logs/alerts.
6. Long jobs report progress via the DB, not the result backend.
7. Never block the loop in a task either (`asyncio.to_thread` for sync libs).

---

## 11. API conventions

### 11.1 Versioning and routing
`/api/v1/...` aggregated in `app/api/v1/router.py`. Breaking changes → `/api/v2`. Health routes are unversioned.

### 11.2 Error format
One shape for all errors:

```json
{"error": {"code": "user_not_found", "message": "User not found", "details": null}, "request_id": "..."}
```

```python
# app/core/exceptions.py  (excerpt)
class AppError(Exception):
    """Base for all expected errors. Subclasses set status_code and code."""
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, details: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
```

Register handlers for `AppError`, `RequestValidationError` (normalized to the same shape), and a catch-all that logs the traceback and returns a generic 500. **Never leak stack traces or SQL in responses.** Module errors subclass `AppError` (`UserNotFoundError(NotFoundError)`).

### 11.3 Pagination
`limit`/`offset` with a hard cap (`limit ≤ 100`), response `Page[T]` = `{items, total, limit, offset}`. For large or fast-growing tables, switch that endpoint to keyset (cursor) pagination.

### 11.4 Health
- `GET /health/live` — process is up. No dependencies. Used by Docker `HEALTHCHECK`.
- `GET /health/ready` — `SELECT 1` on Postgres + `PING` on `redis-broker`. Returns `503` on failure. **`redis-cache` is intentionally excluded:** the cache is an optimization, and its failure must not take pods out of rotation.

### 11.5 Authentication (reference module `auth`)
- Register / login / refresh / logout.
- Passwords: argon2 via `pwdlib`, run through `asyncio.to_thread` (CPU-bound).
- Access token: JWT, 15 min. Refresh token: JWT with a `jti`, 7 days, **rotated on use**. Active `jti`s are stored in Postgres (durable) or in `redis-cache` with a TTL equal to the token lifetime. With the cache option, an evicted entry forces a re-login, which is acceptable; choose Postgres if it is not.
- Logout/revocation: delete the refresh `jti`.
- `CurrentUserDep` dependency decodes the access token and loads the user; authorization checks live in services, not routers.

### 11.6 Middleware (order matters, outermost first)
1. Request ID (`X-Request-ID` in/out, bound to structlog contextvars)
2. Access log (method, path, status, duration; **no bodies, no auth headers**)
3. Security headers (`X-Content-Type-Options`, `Referrer-Policy`, HSTS behind TLS)
4. `CORSMiddleware` with an **explicit origin list** from settings (never `*` with credentials)
5. `TrustedHostMiddleware` in production

### 11.7 Logging
`structlog` configured once in `core/logging.py`: JSON in non-local environments, console renderer locally; stdlib/uvicorn loggers routed through it. Always log with key/value pairs (`log.info("user_created", user_id=...)`). Never log passwords, tokens, or full request bodies.

### 11.8 Rate limiting (optional, off by default)
Prefer the reverse proxy/API gateway. If needed in-app, implement a Redis fixed/sliding-window dependency on `redis-cache`.

---

## 12. Security checklist

- [ ] No secrets in the repo or image; `.env` git-ignored
- [ ] Startup validation rejects placeholder secrets in production
- [ ] Docs/OpenAPI disabled in production
- [ ] CORS origins explicit
- [ ] Password hashing = argon2; JWT secret ≥ 32 random bytes
- [ ] Container runs as a **non-root** user
- [ ] Parameterized queries only (the ORM does this; never f-string SQL)
- [ ] Input validated by Pydantic with bounded lengths and `limit` caps
- [ ] Error responses don't leak internals
- [ ] Dependencies audited in CI (e.g. `uv run pip-audit`)
- [ ] Redis not exposed publicly; Postgres not exposed publicly in production
- [ ] TLS terminates at the reverse proxy/load balancer (not included in this repo)

---

## 13. Testing

### 13.1 Layout and tiers

| Tier | Location | Infrastructure | Purpose |
|---|---|---|---|
| Unit | `tests/unit/` | none (fakes) | Service logic, validators |
| Integration | `tests/integration/` | real Postgres + Redis | Repositories, cache behavior, migrations |
| API | `tests/modules/` | real Postgres + Redis | Full request → response |

Use **real Postgres and Redis**, not SQLite or mocks, for anything that touches data: SQLite differs in types, constraints, and locking.

### 13.2 pytest config

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"   # VERIFY option names for your pytest-asyncio version
asyncio_default_test_loop_scope = "session"      # one event loop for the whole run
testpaths = ["tests"]
addopts = "-ra --strict-markers --cov=app --cov-report=term-missing --cov-fail-under=80"
```

One shared loop avoids asyncpg "attached to a different loop" errors when the engine fixture is session-scoped.

### 13.3 Core fixtures

```python
# tests/conftest.py
"""Test infrastructure: isolated DB per test via SAVEPOINT rollback, DI overrides."""
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.database import Base, get_session
from app.main import app

TEST_DATABASE_URL = "postgresql+asyncpg://app:app@localhost:5432/app_test"


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DATABASE_URL)
    async with eng.begin() as conn:                # simplest: create schema from models.
        await conn.run_sync(Base.metadata.create_all)  # Migrations are verified separately in CI.
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Everything a test does is rolled back, including the service's own commit()."""
    async with engine.connect() as conn:
        outer = await conn.begin()
        async with AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as s:
            yield s
        await outer.rollback()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    # ASGITransport does not run lifespan: set app.state resources here
    # (e.g. app.state.redis_cache = <redis on db 15>) or use the asgi-lifespan package.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
```

`docker/postgres/init-test-db.sql` (mounted into Postgres's init dir) creates the `app_test` database. Use Redis **DB 15** for tests and `FLUSHDB` in a fixture.

### 13.4 Mandatory tests in the boilerplate

- `users`: create, get (cache miss then hit), update (cache invalidated), not-found → 404 error shape
- `auth`: register, login, refresh rotation, invalid token → 401
- Cache: Redis down → request still succeeds (patch the client to raise `RedisError`)
- Worker smoke test: task resolves `get_user_service` via `TaskiqDepends` and runs against the test DB (use `InMemoryBroker` + `taskiq_fastapi.populate_dependency_context`)
- Health: `/health/ready` returns 503 when DB is unreachable
- Migrations (CI): `alembic upgrade head` on an empty DB, then `alembic check`

---

## 14. Code quality and tooling

```toml
# pyproject.toml (excerpt)
[tool.uv]
package = false          # application, not a distributable package

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "ASYNC", "S", "N", "SIM", "RUF", "C4", "PT"]
# Do NOT enable "TCH": FastAPI/Pydantic need annotations importable at runtime.

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]    # asserts are fine in tests

[tool.mypy]
strict = true
plugins = ["pydantic.mypy"]
```

```makefile
# Makefile
.PHONY: up down migrate revision test check fmt
up:        ; docker compose up -d --build
down:      ; docker compose down
migrate:   ; uv run alembic upgrade head
revision:  ; uv run alembic revision --autogenerate -m "$(m)"
test:      ; uv run pytest
fmt:       ; uv run ruff format . && uv run ruff check --fix .
check:     ; uv run ruff format --check . && uv run ruff check . && uv run mypy app
```

`.pre-commit-config.yaml`: ruff (lint + format), `check-merge-conflict`, `end-of-file-fixer`, `detect-private-key`. Install with `uv run pre-commit install`.

---

## 15. Docker and deployment

### 15.1 Dockerfile (multi-stage, uv)

```dockerfile
# syntax=docker/dockerfile:1.7

# ---------- base: python + uv ----------
FROM python:3.12-slim-bookworm AS base
# Pin the uv image to a specific version tag in your repo (do not use :latest).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1
WORKDIR /app

# ---------- dev: includes dev dependency group, used by docker-compose.yml ----------
FROM base AS dev
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project
ENV PATH="/app/.venv/bin:$PATH"
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------- builder: production dependencies only ----------
FROM base AS builder
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# ---------- runtime: minimal, non-root ----------
FROM python:3.12-slim-bookworm AS runtime
RUN groupadd --system app && useradd --system --gid app --no-create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"
# --proxy-headers: trust X-Forwarded-* from the reverse proxy. Set WEB_CONCURRENCY for workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
```

`.dockerignore` must exclude `.venv`, `.git`, `.env*` (except `.env.example`), `__pycache__`, `tests`, `.mypy_cache`, `.ruff_cache`.

### 15.2 `docker-compose.yml` (development)

```yaml
name: fastapi-boilerplate

x-app: &app
  build: { context: ., target: dev }
  env_file: .env
  depends_on:
    postgres:     { condition: service_healthy }
    redis-cache:  { condition: service_healthy }
    redis-broker: { condition: service_healthy }
    migrate:      { condition: service_completed_successfully }
  volumes: ["./app:/app/app", "./tests:/app/tests", "./migrations:/app/migrations"]

services:
  postgres:
    image: postgres:17-alpine           # pin the major; upgrade deliberately
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    ports: ["5432:5432"]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./docker/postgres/init-test-db.sql:/docker-entrypoint-initdb.d/init-test-db.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 5s
      timeout: 3s
      retries: 10

  redis-cache:
    image: redis:7-alpine
    command: ["redis-server", "--maxmemory", "256mb", "--maxmemory-policy", "allkeys-lru", "--save", ""]
    ports: ["6379:6379"]
    healthcheck: { test: ["CMD", "redis-cli", "ping"], interval: 5s, timeout: 3s, retries: 10 }

  redis-broker:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]
    volumes: ["brokerdata:/data"]
    healthcheck: { test: ["CMD", "redis-cli", "ping"], interval: 5s, timeout: 3s, retries: 10 }

  migrate:                               # one-shot: apply migrations, then exit
    build: { context: ., target: dev }
    env_file: .env
    command: ["alembic", "upgrade", "head"]
    depends_on: { postgres: { condition: service_healthy } }

  api:
    <<: *app
    ports: ["8000:8000"]

  worker:
    <<: *app
    command: ["taskiq", "worker", "app.worker.broker:broker", "app.worker.tasks"]

  scheduler:
    <<: *app
    command: ["taskiq", "scheduler", "app.worker.scheduler:scheduler", "app.worker.tasks"]

volumes:
  pgdata:
  brokerdata:
```

### 15.3 `docker-compose.prod.yml` (production differences)

```yaml
# Run: docker compose -f docker-compose.prod.yml up -d
x-app: &app
  image: ${IMAGE}                       # built and pushed by CI, tagged with the git SHA
  env_file: .env.production             # or platform secrets; never baked into the image
  restart: unless-stopped
  init: true                            # reap zombie processes
  stop_grace_period: 30s                # let in-flight requests/tasks finish
  depends_on:
    redis-cache:  { condition: service_healthy }
    redis-broker: { condition: service_healthy }

services:
  migrate:
    image: ${IMAGE}
    env_file: .env.production
    command: ["alembic", "upgrade", "head"]
    restart: "no"

  api:
    <<: *app
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000",
              "--proxy-headers", "--workers", "${WEB_CONCURRENCY:-2}",
              "--timeout-graceful-shutdown", "25"]
    depends_on:
      migrate: { condition: service_completed_successfully }
    ports: ["127.0.0.1:8000:8000"]      # reverse proxy on the host terminates TLS
    deploy: { resources: { limits: { cpus: "1.0", memory: 512M } } }

  worker:
    <<: *app
    command: ["taskiq", "worker", "app.worker.broker:broker", "app.worker.tasks", "--workers", "2"]
    depends_on:
      migrate: { condition: service_completed_successfully }

  scheduler:                            # exactly one instance
    <<: *app
    command: ["taskiq", "scheduler", "app.worker.scheduler:scheduler", "app.worker.tasks"]
    depends_on:
      migrate: { condition: service_completed_successfully }

  redis-cache:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--maxmemory", "512mb", "--maxmemory-policy", "allkeys-lru", "--save", ""]
    healthcheck: { test: ["CMD", "redis-cli", "ping"], interval: 10s, timeout: 3s, retries: 5 }

  redis-broker:
    image: redis:7-alpine
    restart: unless-stopped
    command: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]
    volumes: ["brokerdata:/data"]
    healthcheck: { test: ["CMD", "redis-cli", "ping"], interval: 10s, timeout: 3s, retries: 5 }

volumes:
  brokerdata:
```

Prefer **managed PostgreSQL** in production. If you must self-host on one machine, add a `postgres` service with a named volume and a backup job; backups are your responsibility. Confirm the `taskiq worker --workers` flag against the installed version (**VERIFY**).

### 15.4 Deployment procedure

1. CI builds image `runtime` target → pushes tagged with the git SHA.
2. On the target: `IMAGE=<registry>/<repo>:<sha> docker compose -f docker-compose.prod.yml pull`.
3. `docker compose ... up -d` — `migrate` runs first and must succeed, then API/worker/scheduler start.
4. Verify `/health/ready`, check logs for errors.
5. **Rollback:** redeploy the previous SHA. Because migrations are backward-compatible ([§8.3](#83-rules) rule 4), the previous code runs on the new schema.

On Kubernetes the mapping is: `migrate` → Job (or Helm pre-upgrade hook), `api`/`worker` → Deployments, `scheduler` → Deployment with `replicas: 1` (`strategy: Recreate`), probes → `/health/live` and `/health/ready`.

---

## 16. CI

```yaml
# .github/workflows/ci.yml
name: ci
on: { pull_request: {}, push: { branches: [main] } }

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17-alpine
        env: { POSTGRES_USER: app, POSTGRES_PASSWORD: app, POSTGRES_DB: app_test }
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U app" --health-interval 5s --health-timeout 3s --health-retries 10
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
    env:
      ENVIRONMENT: test
      DATABASE_URL: postgresql+asyncpg://app:app@localhost:5432/app_test
      REDIS_CACHE_URL: redis://localhost:6379/15
      REDIS_BROKER_URL: redis://localhost:6379/14
      JWT_SECRET_KEY: ci-only-secret-not-for-production-0123456789
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6        # VERIFY: use the current major of each action
        with: { enable-cache: true }
      - run: uv sync --frozen
      - run: uv run ruff format --check . && uv run ruff check .
      - run: uv run mypy app
      - run: uv run alembic upgrade head   # migrations apply on an empty DB
      - run: uv run alembic check          # models == migrations
      - run: uv run pytest

  image:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build --target runtime -t app:ci .
```

Add image push + deploy steps for your environment. Branch protection: `test` and `image` required, one approving review.

---

## 17. Adding a new module

Copy `users/` as the template. Checklist for module `orders`:

1. Create `app/modules/orders/` with `router.py schemas.py models.py repository.py service.py dependencies.py exceptions.py` (and `tasks.py` if needed).
2. **models.py** — inherit `Base`, `UUIDPrimaryKeyMixin`, `TimestampMixin`; `__tablename__` plural snake_case.
3. Import the model in `app/models.py`.
4. `make revision m="add orders"` → **review** → `make migrate`.
5. **repository.py** — subclass `BaseRepository`; queries only.
6. **schemas.py** — separate `Create`, `Update`, `Read` models; `Read` uses `model_config = ConfigDict(from_attributes=True)`.
7. **service.py** — business logic; `commit()` here; cache read/invalidate; enqueue tasks **after** commit; raise `AppError` subclasses.
8. **dependencies.py** — `get_orders_repository`, `get_orders_service`, `OrdersServiceDep`.
9. **router.py** — thin handlers with `response_model` and explicit status codes.
10. Include the router in `app/api/v1/router.py`.
11. If it has tasks: import them in `app/worker/tasks.py`.
12. Tests: unit (service with fakes) + API tests (happy path, validation error, not found, auth).
13. `make check && make test` green before opening the PR.

### PR checklist
- [ ] Migration reviewed; single Alembic head
- [ ] No sync I/O inside `async def`
- [ ] No lazy loading; relations loaded explicitly
- [ ] Cache keys versioned with TTL; invalidation after commit
- [ ] Tasks idempotent and enqueued after commit
- [ ] New env vars added to `Settings` **and** `.env.example`
- [ ] Tests cover success + failure paths

---

## 18. Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `MissingGreenlet` | Lazy-loaded relation in async code | `selectinload`/`joinedload`; return schemas from services |
| API latency spikes under load | Blocking call in `async def` (`requests`, `time.sleep`, hashing) | `httpx.AsyncClient`, `asyncio.sleep`, `asyncio.to_thread` |
| `QueuePool limit reached` | Pool × workers × replicas too large, or leaked sessions | Size the pool ([§7.3](#73-rules)); always use the dependency |
| `attached to a different loop` in tests | Session-scoped engine with function-scoped loop | Session-scoped loop ([§13.2](#132-pytest-config)) |
| Stale cache after update | Invalidated before commit, or missing key | Commit first, delete after; keep TTL as safety net |
| Task runs before the row exists | Enqueued before commit | Enqueue after `commit()` |
| Cron job runs twice | More than one scheduler replica | Exactly one scheduler |
| Queued tasks vanish | Broker Redis has an eviction policy | Dedicated `redis-broker` with `noeviction` + AOF |
| Autogenerate wants to drop+add a renamed column | Alembic can't detect renames | Hand-edit to `op.alter_column(..., new_column_name=...)` |
| `Depends` in task fails on `Request` | Missing `TaskiqDepends` marker | `Annotated[Request, TaskiqDepends()]` ([§10.4](#104-using-dependencies-in-tasks)) |

---

## 19. Acceptance checklist

The boilerplate is **not ready for handover** until every item is true on a clean clone:

**Setup**
- [ ] `cp .env.example .env && docker compose up --build` brings up everything with zero manual steps
- [ ] `uv sync && make check && make test` passes locally
- [ ] README links to this guide; `.env.example` is complete

**Functionality**
- [ ] `users` and `auth` modules work end to end via `/docs`
- [ ] A cached read hits Redis on the second call (verified in a test)
- [ ] Killing `redis-cache` does not break API responses
- [ ] A task enqueued from the API is executed by the worker, and the scheduler's cron task fires
- [ ] `/health/ready` reflects DB and broker status

**Quality**
- [ ] Coverage ≥ 80%; mypy strict clean; ruff clean
- [ ] `alembic upgrade head` on an empty DB, `alembic check` clean, one head
- [ ] CI green on a fresh PR

**Production**
- [ ] `docker build --target runtime` image runs as non-root
- [ ] Booting with `ENVIRONMENT=production` and the default JWT secret **fails**
- [ ] `docs-url` is disabled in production
- [ ] `docker-compose.prod.yml` deploys, migrates, and rolls back (tested once)
- [ ] Logs are JSON with request IDs; no secrets in logs

---

## 20. Deliberately excluded

Not in the core because not every project needs them. Add via ADR when justified.

| Excluded | Add when |
|---|---|
| Multi-tenancy | The product is B2B SaaS with tenant isolation requirements |
| RBAC/permissions framework | More than 2–3 roles exist |
| Email/SMS providers | A real notification feature exists (the stub task shows the pattern) |
| File storage (S3) | Users upload files |
| Prometheus/OpenTelemetry/Sentry | You have somewhere to send the data (add an optional `SENTRY_DSN`/OTel setting) |
| Redis rate limiting | The proxy/gateway can't do it |
| PgBouncer | Connection math in [§7.3](#73-rules) fails |
| WebSockets / SSE | A real-time feature exists |
| GraphQL, gRPC | A consumer requires it |
| Kubernetes manifests / Helm | The deploy target is Kubernetes (mapping in [§15.4](#154-deployment-procedure)) |
| TLS / reverse proxy in Compose | Handled by your platform (Caddy, Nginx, ALB, ...) |

---

## Appendix A — Decision records to create on day one

Create `docs/adr/0001-…` for each, one paragraph each (context, decision, consequences):
1. Feature modules + Router/Service/Repository layering
2. Services own transactions
3. Taskiq over Celery
4. Split Redis (cache vs broker)
5. Cache-aside in services, no decorators
6. Migrations as a separate deploy step

## Appendix B — Environment variable reference

| Variable | Required | Default | Notes |
|---|---|---|---|
| `ENVIRONMENT` | no | `local` | `local`/`test`/`staging`/`production` |
| `DEBUG` | no | `false` | Must be `false` in production |
| `LOG_LEVEL` / `LOG_JSON` | no | `INFO` / `true` | `LOG_JSON=false` for local console |
| `CORS_ORIGINS` | no | `[]` | JSON list |
| `DATABASE_URL` | **yes** | — | `postgresql+asyncpg://…` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | no | `10` / `10` | See pool sizing |
| `REDIS_CACHE_URL` | **yes** | — | Eviction-enabled instance |
| `REDIS_BROKER_URL` | **yes** | — | `noeviction` + AOF instance |
| `CACHE_PREFIX` / `CACHE_DEFAULT_TTL_SECONDS` | no | `app` / `300` | |
| `JWT_SECRET_KEY` | **yes** | — | ≥ 32 random bytes in production |
| `ACCESS_TOKEN_TTL_MINUTES` / `REFRESH_TOKEN_TTL_DAYS` | no | `15` / `7` | |
| `WEB_CONCURRENCY` | no | `2` (prod compose) | Uvicorn workers per API container |