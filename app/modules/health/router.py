from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])

@router.get("/live")
async def health():
    return {
        "status": "ok"
    }