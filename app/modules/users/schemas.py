"""Pydantic request/response models for the users module.

Request models use `extra="forbid"`: unknown fields (e.g. `is_superuser` sent to a
self-service endpoint) are rejected with 422 instead of being silently ignored.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, StringConstraints

PASSWORD_MIN_LENGTH = 10
PASSWORD_MAX_LENGTH = 128  # upper bound also caps argon2 CPU cost per request


def _normalize_email(value: str) -> str:
    return value.strip().lower()


# Emails are compared case-insensitively by always storing/looking up the lower-cased form.
Email = Annotated[EmailStr, AfterValidator(_normalize_email)]
Passowrd = Annotated[
    str, StringConstraints(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
]
FullName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserCreate(_Request):
    """Self-registration payload (also the base for admin creation)."""

    email: Email
    password: Passowrd
    full_name: FullName


class AdminUserCreate(UserCreate):
    """Admin-created account: may set flags that self-registration cannot."""

    is_active: bool = True
    is_superuser: bool = True


class UserUpdateMe(_Request):
    """Fields a user may change on their own profile."""

    full_name: FullName | None = None


class UserAdminUpdate(_Request):
    """Fields an administrator may change on any user."""

    full_name: FullName | None = None
    is_active: bool | None = None
    is_superuser: bool | None = None


class UserRead(BaseModel):
    """Public representation of a user. Never contains the password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: FullName
    is_active: bool
    is_superuser: bool
    created_at: datetime
    updated_at: datetime


class UserCredentials(BaseModel):
    """INTERNAL: what the auth module needs to verify a login. Never return from a router."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    password_hash: str
    is_active: bool
