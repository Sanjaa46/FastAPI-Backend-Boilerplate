from fastapi import FastAPI

from app.api.v1.router import api_v1_router
app = FastAPI(
    title="FastAPI Boilerplate",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.include_router(api_v1_router)