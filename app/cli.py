"""Operational commands. Usage:

    uv run python -m app.cli create-superuser --email admin@example.com --full-name "Admin"

The password is read from the ADMIN_PASSWORD environment variable if set, otherwise prompted
(hidden). Needs the same environment (.env) as the API: PostgreSQL and both Redis instances.
"""

import argparse
import asyncio
import getpass
import os
import sys

from pydantic import ValidationError

from app.core.cache import CacheService
from app.core.config import get_settings
from app.core.database import create_engine, create_session_factory
from app.core.logging import configure_logging
from app.core.redis import create_redis
from app.modules.users.exceptions import EmailAlreadyExistsError
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import AdminUserCreate
from app.modules.users.service import UserService
from app.worker import tasks as _tasks  # noqa: F401  # registers tasks for the welcome email
from app.worker.broker import broker


async def create_superuser(email: str, full_name: str, password: str) -> int:
    """Create an active superuser through UserService (same rules as the API). Returns exit code."""
    settings = get_settings()
    configure_logging(settings)
    try:
        payload = AdminUserCreate.model_validate(
            {
                "email": email,
                "password": password,
                "full_name": full_name,
                "is_superuser": True,
                "is_active": True,
            }
        )
    except ValidationError as exc:
        for err in exc.errors():
            print(f"invalid {'.'.join(str(p) for p in err['loc'])}: {err['msg']}", file=sys.stderr)
        return 2

    engine = create_engine(settings)
    redis = create_redis(str(settings.redis_cache_url))
    await broker.startup()  # so the welcome-email task can be enqueued like in the API
    try:
        async with create_session_factory(engine)() as session:
            cache = CacheService(
                redis,
                prefix=f"{settings.cache_prefix}:users",
                default_ttl=settings.cache_default_ttl_seconds,
            )
            service = UserService(UserRepository(session), cache, session)
            try:
                user = await service.create_user(payload)
            except EmailAlreadyExistsError:
                print(f"a user with email {payload.email} already exists", file=sys.stderr)
                return 1
        print(f"created superuser {user.email} ({user.id})")
        return 0
    finally:
        await broker.shutdown()
        await redis.aclose()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    su = sub.add_parser("create-superuser", help="create an administrator account")
    su.add_argument("--email", required=True)
    su.add_argument("--full-name", required=True)
    args = parser.parse_args()

    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
    return asyncio.run(create_superuser(args.email, args.full_name, password))


if __name__ == "__main__":
    raise SystemExit(main())
