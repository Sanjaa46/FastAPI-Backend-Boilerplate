from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import BaseModel, Field

MAX_PAGE_SIZE = 100

class PageParams(BaseModel):
    limit: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0)


def _page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


PageParamsDep = Annotated[PageParams, Depends(_page_params)]


class Page[T](BaseModel):
    """One page of results plus the total number of matching rows."""
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Any | None = None

class ErrorResponse(BaseModel):
    """Shape of every error response (see app.core.exceptions)."""

    error: ErrorBody
    request_id: str | None = None

def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses=` entries documenting the shared error envelope."""
    return {code: {"model": ErrorResponse, "description": "Error"} for code in status_codes}