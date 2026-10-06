"""Password hashing and JWT primitives. No business rules, no database access.

`hash_password` / `verify_password` are CPU-bound (argon2): call them through
`asyncio.to_thread` from async code so the event loop is never blocked.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Literal, cast

import jwt
from pwdlib import PasswordHash

from app.core.config import Settings
from app.core.exceptions import InvalidTokenError

TokenType = Literal["access", "refresh"]

# PasswordHash.recommended() = argon2id with library-default (OWASP-aligned) parameters
_password_hash = PasswordHash.recommended()


def hash_password(plain: str) -> str:
    return _password_hash.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _password_hash.verify(plain, hashed)


@cache
def _dummy_hash() -> str:
    """A valid hash of a throwaway value, computed once per process."""
    return hash_password("timing-equalizer-not-a-real-password")


def verify_dummy_password(plain: str) -> None:
    """Burn the same CPU as a real verification.

    Called when the account does not exist so response time does not reveal which emails
    are registered (user-enumeration timing attack).
    """
    _password_hash.verify(plain, _dummy_hash())


@dataclass
class TokenPayload:
    subject: uuid.UUID
    jti: uuid.UUID
    type: TokenType
    expires_at: datetime


def create_token(
    *,
    subject: uuid.UUID,
    kind: TokenType,
    expires_delta: timedelta,
    settings: Settings,
    jti: uuid.UUID | None = None,
) -> tuple[str, TokenPayload]:
    """Sign a JWT. Returns the encoded token and the claims it carries"""
    now = datetime.now(UTC)
    payload = TokenPayload(
        subject=subject,
        jti=jti or uuid.uuid4(),
        type=kind,
        expires_at=now + expires_delta,
    )
    claims = {
        "sub": str(payload.subject),
        "jti": str(payload.jti),
        "type": payload.type,
        "iat": now,
        "exp": payload.expires_at,
    }
    encoded = jwt.encode(
        claims, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return encoded, payload


def decode_token(token: str, settings: Settings, *, expected_type: TokenType) -> TokenPayload:
    """Verify signature, expiry and token type. Raises InvalidTokenError on ANY problem.

    The algorithm is pinned from settings (never read from the token header), and `exp`,
    `sub`, `jti` and `type` are mandatory claims.
    """
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "jti", "type"]},
        )
        token_type = claims["type"]
        if token_type != expected_type:
            raise InvalidTokenError("Wrong token type")
        return TokenPayload(
            subject=uuid.UUID(claims["sub"]),
            jti=uuid.UUID(claims["jti"]),
            type=cast(TokenType, token_type),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        )
    except InvalidTokenError:
        raise
    except (jwt.PyJWTError, ValueError, KeyError, TypeError) as exc:
        raise InvalidTokenError() from exc
