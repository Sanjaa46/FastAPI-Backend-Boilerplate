import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BeforeValidator, PostgresDsn, RedisDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_RATE_RE = re.compile(r"(\d+)\s*/\s*(\d+)?\s*(second|minute|hour|day)s?", re.IGNORECASE)
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


@dataclass(frozen=True, slots=True)
class Rate:
    """`limit` requests per `window_seconds`. Written "100/minute" or "10/15 minutes"."""

    limit: int
    window_seconds: int


def _parse_rate(value: object) -> Rate:
    if isinstance(value, Rate):
        return value
    match = _RATE_RE.fullmatch(str(value).strip())
    if match is None:
        raise ValueError(f"invalid rate {value!r}: use '<count>/<unit>', e.g. '100/minute'")
    count, multiple, unit = match.groups()
    limit, window_seconds = int(count), int(multiple or 1) * _UNIT_SECONDS[unit.lower()]
    if limit < 1 or window_seconds < 1:
        raise ValueError(f"invalid rate {value!r}: count and window must be at least 1")
    return Rate(limit, window_seconds)


# NoDecode: pydantic-settings would otherwise try to read the env value as JSON (a dataclass
# counts as a complex type) and crash on "100/minute".
RateSetting = Annotated[Rate, NoDecode, BeforeValidator(_parse_rate)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- App ---
    app_name: str = "fastapi-boilerplate"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = []  # env: CORS_ORIGINS='["https://app.example.com"]'
    trusted_hosts: list[str] = []  # env: TRUSTED_HOSTS='["api.example.com"]' (production only)

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True  # False => human-readable console output

    # --- PostgreSQL ---
    database_url: PostgresDsn  # postgresql+asyncpg://user:pass@host:5432/db
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800

    # --- Redis (two instance) ---
    redis_cache_url: RedisDsn
    redis_broker_url: RedisDsn

    # --- Cache ---
    cache_prefix: str = "app"
    cache_default_ttl_seconds: int = 300

    # --- Auth ---
    jwt_secret_key: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    registration_enabled: bool = True  # false => POST /auth/register returns 403

    # --- Rate limiting (sliding window on redis-cache; see app/core/rate_limit.py) ---
    # Rates: "<count>/<unit>" or "<count>/<n> <unit>s", e.g. "100/minute", "10/15 minutes".
    rate_limit_enabled: bool = False  # opt-in: RATE_LIMIT_ENABLED=true
    rate_limit_ip: RateSetting = Rate(300, 60)  # every /api/v1 request, per client IP
    rate_limit_user: RateSetting = Rate(120, 60)  # every authenticated request, per user
    rate_limit_login: RateSetting = Rate(10, 60)  # POST /auth/login, per client IP
    rate_limit_login_account: RateSetting = Rate(10, 900)  # POST /auth/login, per target email
    rate_limit_register: RateSetting = Rate(5, 3600)  # POST /auth/register, per client IP
    rate_limit_change_password: RateSetting = Rate(5, 900)  # POST /auth/change-password, per user

    @model_validator(mode="after")
    def _reject_unsafe_production(self) -> "Settings":
        """Fail fast: never boot production with a placeholder secret or debug on"""
        if self.environment == "production":
            if self.debug:
                raise ValueError("DEBUG must be false in production")
            if self.jwt_secret_key.get_secret_value() in {"", "change_me"}:
                raise ValueError("JWT_SECRET_KEY must be set to a strong secret in production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor. Override in tests via dependency_overrides."""
    return Settings()  # values come from the environment / .env
