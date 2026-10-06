"""Rate-limit building blocks that need no I/O: rate parsing, client IP, user key."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request

from app.core.config import Rate, RateSetting, Settings
from app.core.rate_limit import by_ip, by_user, client_ip
from app.core.security import create_token

_rate = TypeAdapter(RateSetting)


def _request(client: tuple[str, int] | None, headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "client": client, "headers": raw})


# --------------------------------------------------------------------------- Rate parsing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100/minute", Rate(100, 60)),
        ("5/Hour", Rate(5, 3600)),
        ("1/second", Rate(1, 1)),
        ("20 / day", Rate(20, 86400)),
        ("10/15 minutes", Rate(10, 900)),
        ("10/15minutes", Rate(10, 900)),
    ],
)
def test_rate_parses(text: str, expected: Rate) -> None:
    assert _rate.validate_python(text) == expected


@pytest.mark.parametrize(
    "text", ["", "100", "minute", "100/week", "0/minute", "5/0 minutes", "-1/minute", "5/m"]
)
def test_rate_rejects_garbage(text: str) -> None:
    with pytest.raises(ValidationError):
        _rate.validate_python(text)


def test_rates_load_from_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real env path: pydantic-settings must not try to JSON-decode "50/minute"."""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_IP", "50/15 minutes")
    monkeypatch.setenv("RATE_LIMIT_LOGIN", "7/minute")
    loaded = Settings(_env_file=None)
    assert loaded.rate_limit_enabled is True
    assert loaded.rate_limit_ip == Rate(50, 900)
    assert loaded.rate_limit_login == Rate(7, 60)
    assert loaded.rate_limit_register == Rate(5, 3600)  # untouched default


def test_rates_load_from_a_dotenv_file(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("RATE_LIMIT_LOGIN_ACCOUNT=10/15 minutes\nRATE_LIMIT_USER=60/minute\n")
    loaded = Settings(_env_file=dotenv)
    assert loaded.rate_limit_login_account == Rate(10, 900)
    assert loaded.rate_limit_user == Rate(60, 60)


def test_an_invalid_rate_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_LOGIN", "often")
    with pytest.raises(ValidationError, match="rate_limit_login"):
        Settings(_env_file=None)


# --------------------------------------------------------------------------- client IP


def test_client_ip_ipv4_is_used_as_is() -> None:
    assert client_ip(_request(("203.0.113.7", 5000))) == "203.0.113.7"


def test_client_ip_unwraps_ipv4_mapped_ipv6() -> None:
    assert client_ip(_request(("::ffff:203.0.113.7", 5000))) == "203.0.113.7"


def test_client_ip_buckets_ipv6_by_slash_64() -> None:
    a = client_ip(_request(("2001:db8:1:2:aaaa::1", 1)))
    b = client_ip(_request(("2001:db8:1:2:bbbb::9", 1)))
    other = client_ip(_request(("2001:db8:1:3::1", 1)))
    assert a == b == "2001:db8:1:2::/64"
    assert other != a


def test_client_ip_without_a_client_or_with_a_non_ip_host() -> None:
    assert client_ip(_request(None)) == "unknown"
    assert client_ip(_request(("/run/app.sock", 0))) == "/run/app.sock"


def test_client_ip_ignores_client_supplied_forwarding_headers(settings: Settings) -> None:
    """Only uvicorn (from FORWARDED_ALLOW_IPS) may rewrite the address, never the app."""
    request = _request(
        ("203.0.113.7", 1), {"X-Forwarded-For": "198.51.100.1", "X-Real-IP": "1.1.1.1"}
    )
    assert by_ip(request, settings) == "203.0.113.7"


# --------------------------------------------------------------------------- user key


def _token(settings: Settings, kind: str = "access", ttl: timedelta = timedelta(minutes=5)) -> str:
    token, _ = create_token(
        subject=uuid.UUID(int=7),
        kind=kind,  # type: ignore[arg-type]
        expires_delta=ttl,
        settings=settings,
    )
    return token


def test_by_user_returns_the_token_subject(settings: Settings) -> None:
    request = _request(("203.0.113.7", 1), {"Authorization": f"Bearer {_token(settings)}"})
    assert by_user(request, settings) == str(uuid.UUID(int=7))


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer not-a-jwt", "Basic dXNlcjpwYXNz", "Token abc"],
)
def test_by_user_skips_anonymous_and_malformed(settings: Settings, header: str | None) -> None:
    request = _request(
        ("203.0.113.7", 1), {"Authorization": header} if header is not None else None
    )
    assert by_user(request, settings) is None


def test_by_user_rejects_refresh_and_expired_tokens(settings: Settings) -> None:
    for token in (_token(settings, "refresh"), _token(settings, ttl=timedelta(seconds=-5))):
        request = _request(("203.0.113.7", 1), {"Authorization": f"Bearer {token}"})
        assert by_user(request, settings) is None
