from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
