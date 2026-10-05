"""Unit tests for password hashing and JWT primitives (no I/O)."""

import uuid
from datetime import timedelta

import jwt
import pytest

from app.core.config import Settings
from app.core.exceptions import InvalidTokenError
from app.core.security import (
    create_token,
    decode_token,
    hash_password,
    verify_dummy_password,
    verify_password,
)

USER_ID = uuid.uuid4()


def test_hash_and_verify_roundtrip() -> None:
    hashed = hash_password("a-long-enough-password")
    assert hashed != "a-long-enough-password"
    assert verify_password("a-long-enough-password", hashed)
    assert not verify_password("a-different-password", hashed)


def test_hashes_are_salted() -> None:
    assert hash_password("same-password-twice") != hash_password("same-password-twice")


def test_verify_dummy_password_never_raises() -> None:
    verify_dummy_password("anything")


def test_token_roundtrip(settings: Settings) -> None:
    token, issued = create_token(
        subject=USER_ID, kind="access", expires_delta=timedelta(minutes=5), settings=settings
    )
    claims = decode_token(token, settings, expected_type="access")
    assert claims.subject == USER_ID
    assert claims.jti == issued.jti
    assert claims.type == "access"


def test_expired_token_rejected(settings: Settings) -> None:
    token, _ = create_token(
        subject=USER_ID, kind="access", expires_delta=timedelta(seconds=-5), settings=settings
    )
    with pytest.raises(InvalidTokenError):
        decode_token(token, settings, expected_type="access")


def test_wrong_token_type_rejected(settings: Settings) -> None:
    token, _ = create_token(
        subject=USER_ID, kind="refresh", expires_delta=timedelta(minutes=5), settings=settings
    )
    with pytest.raises(InvalidTokenError):
        decode_token(token, settings, expected_type="access")


def test_tampered_signature_rejected(settings: Settings) -> None:
    token, _ = create_token(
        subject=USER_ID, kind="access", expires_delta=timedelta(minutes=5), settings=settings
    )
    head, payload, signature = token.split(".")
    tampered = ".".join(
        [head, payload, signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")]
    )
    with pytest.raises(InvalidTokenError):
        decode_token(tampered, settings, expected_type="access")


def test_token_signed_with_other_secret_rejected(settings: Settings) -> None:
    forged = jwt.encode(
        {"sub": str(USER_ID), "jti": str(uuid.uuid4()), "type": "access", "exp": 4_000_000_000},
        "some-other-secret-some-other-secret-00",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_token(forged, settings, expected_type="access")


def test_unsigned_alg_none_token_rejected(settings: Settings) -> None:
    unsigned = jwt.encode(
        {"sub": str(USER_ID), "jti": str(uuid.uuid4()), "type": "access", "exp": 4_000_000_000},
        key=None,
        algorithm="none",
    )
    with pytest.raises(InvalidTokenError):
        decode_token(unsigned, settings, expected_type="access")


def test_missing_claims_rejected(settings: Settings) -> None:
    no_jti = jwt.encode(
        {"sub": str(USER_ID), "type": "access", "exp": 4_000_000_000},
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        decode_token(no_jti, settings, expected_type="access")


def test_non_uuid_subject_rejected(settings: Settings) -> None:
    bad_sub = jwt.encode(
        {"sub": "not-a-uuid", "jti": str(uuid.uuid4()), "type": "access", "exp": 4_000_000_000},
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(InvalidTokenError):
        decode_token(bad_sub, settings, expected_type="access")


def test_garbage_rejected(settings: Settings) -> None:
    with pytest.raises(InvalidTokenError):
        decode_token("definitely.not.a-jwt", settings, expected_type="access")
