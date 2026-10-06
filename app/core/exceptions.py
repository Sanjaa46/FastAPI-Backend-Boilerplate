"""Application error hierarchy and the exception handlers that give every error one JSON shape.

Response shape:
    {"error": {"code": "...", "message": "...", "details": ...}, "request_id": "..."}
"""

from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()


class AppError(Exception):
    """Base for all expected errors. Subclasses set status_code and code."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        details: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.headers = headers


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"

    def __init__(self, message: str = "Not authenticated", details: object | None = None) -> None:
        super().__init__(message, details, headers={"WWW-Authenticate": "Bearer"})


class InvalidTokenError(UnauthorizedError):
    code = "invalid_token"

    def __init__(self, message: str = "Invalid or expired token") -> None:
        super().__init__(message)


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class TooManyRequestsError(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, retry_after: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(
            f"Rate limit exceeded. Try again in {retry_after}s.",
            {"retry_after": retry_after},
            headers={"Retry-After": str(retry_after), **(headers or {})},
        )


def _request_id() -> str | None:
    """Request ID bound by RequestContextMiddleware (None outside a request)."""
    value = structlog.contextvars.get_contextvars().get("request_id")
    return str(value) if value is not None else None


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: object | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {"code": code, "message": message, "details": jsonable_encoder(details)},
        "request_id": _request_id(),
    }
    return JSONResponse(status_code=status_code, content=body, headers=headers)


async def _handle_app_error(_: Request, exc: Exception) -> JSONResponse:
    err = cast(AppError, exc)  # registered for Apperror only
    return _error_response(err.status_code, err.code, err.message, err.details, err.headers)


async def _handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    errors = cast(RequestValidationError, exc).errors()  # registered for this type only
    # Drop `input`/`ctx` (can echo secrets such as passwords); keep location + message
    details = [
        {"loc": [str(p) for p in err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in errors
    ]
    return _error_response(422, "validation_error", "Request validation failed", details)


async def _handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    http_exc = cast(StarletteHTTPException, exc)  # register for this type only
    return _error_response(
        http_exc.status_code,
        f"http_{http_exc.status_code}",
        str(http_exc.detail),
        headers=dict(http_exc.headers) if http_exc.headers else None,
    )


async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    # Full traceback goes to the logs; the client only gets a generic message.
    log.error("unhandled_exception", exc_info=exc)
    return _error_response(500, "internal_error", "Internal server error")


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all handlers. Called once from create_app()"""
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
