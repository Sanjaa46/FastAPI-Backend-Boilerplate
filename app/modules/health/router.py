from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.dependencies import SessionDep

log = structlog.get_logger()
router = APIRouter()

class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"

class ReadyResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Literal["ok", "fail"]]

async def get_redis_broker(request: Request) -> Redis:
    redis: Redis = request.app.state.redis_broker
    return redis

@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse()

@router.get("/ready", response_model=ReadyResponse)
async def ready(
    session: SessionDep, broker_redis: Annotated[Redis, Depends(get_redis_broker)]
) -> ReadyResponse | JSONResponse:
    checks: dict[str, Literal["ok", "fail"]] = {}
    try:
        await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        log.warning("readiness_postgres_failed", exc_info=True)
        checks["postgres"] = "fail"
    try:
        await broker_redis.ping()
        checks["redis_broker"] = "ok"
    except Exception:
        log.warnin("readiness_redis_broker_failed", exc_info=True)
        checks["redis_broker"] = "fail"

    if all(v == "ok" for v in checks.values()):
        return ReadyResponse(status="ok", checks=checks)
    body = ReadyResponse(status="unavailable", checks=checks)
    return JSONResponse(status_code=503, content=body.model_dump())