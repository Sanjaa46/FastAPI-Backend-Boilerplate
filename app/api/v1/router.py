from fastapi import APIRouter, Depends

from app.common.schemas import error_responses
from app.core.rate_limit import limit_ip, limit_user
from app.modules.auth.router import router as auth_router
from app.modules.users.router import router as users_router

# Umbrella rate limits for every v1 route (no-ops unless RATE_LIMIT_ENABLED=true). They run
# before each route's own dependencies, so a throttled client never reaches auth or the DB.
api_router = APIRouter(
    dependencies=[Depends(limit_ip), Depends(limit_user)],
    responses=error_responses(429),
)

api_router.include_router(auth_router)
api_router.include_router(users_router)
