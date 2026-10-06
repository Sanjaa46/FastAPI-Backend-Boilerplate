from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.users.schemas import PASSWORD_MAX_LENGTH, Email, Passowrd


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_Request):
    email: Email
    # No min length here: never reveal (or break logins over) a changed password policy.
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class RefreshRequest(_Request):
    refresh_token: str = Field(min_length=1, max_length=2048)


class ChangePasswordRequest(_Request):
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: Passowrd


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth2 scheme name, not a secret)
    expires_in: int = Field(description="Access token lifetime in seconds")
