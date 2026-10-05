"""Cross-cutting behavior: health probes, request IDs, headers, error envelope, worker DI."""

import taskiq_fastapi
from httpx import AsyncClient
from redis.exceptions import RedisError

from app.main import app
from app.modules.auth.tasks import purge_expired_refresh_tokens
from app.modules.health.router import get_redis_broker
from app.worker.broker import broker


async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_ok(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"postgres": "ok", "redis_broker": "ok"}}


async def test_readiness_503_when_broker_redis_is_down(client: AsyncClient) -> None:
    class DownRedis:
        async def ping(self) -> None:
            raise RedisError("down")

    app.dependency_overrides[get_redis_broker] = lambda: DownRedis()
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"postgres": "ok", "redis_broker": "fail"},
    }


async def test_request_id_is_generated_and_echoed(client: AsyncClient) -> None:
    generated = await client.get("/health/live")
    assert len(generated.headers["x-request-id"]) == 32

    echoed = await client.get("/health/live", headers={"X-Request-ID": "trace-abc.123"})
    assert echoed.headers["x-request-id"] == "trace-abc.123"

    # Hostile values are replaced, never reflected.
    hostile = await client.get("/health/live", headers={"X-Request-ID": "bad id\twith spaces"})
    assert hostile.headers["x-request-id"] != "bad id\twith spaces"


async def test_security_headers_present(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_unknown_route_uses_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nope", headers={"X-Request-ID": "req-1"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_404"
    assert response.json()["request_id"] == "req-1"


async def test_openapi_documents_security_scheme(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert "/api/v1/auth/login" in schema["paths"]
    assert "/api/v1/users/{user_id}" in schema["paths"]


async def test_taskiq_tasks_resolve_fastapi_dependencies(client: AsyncClient) -> None:
    """A task using get_session must work through the broker (the DI-in-worker contract)."""
    taskiq_fastapi.populate_dependency_context(broker, app)
    await broker.startup()
    try:
        sent = await purge_expired_refresh_tokens.kiq()
        result = await sent.wait_result(timeout=10)
    finally:
        await broker.shutdown()
    assert not result.is_err, result.error
    assert isinstance(result.return_value, int)
