"""Application configuration.

All variables are prefixed ``FLOW_`` (12-factor, see
docs/non-functional-requirements.md). No hardcoded secrets.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://flow_app:flow_app@localhost:5432/flow",
        description="Async URL (asyncpg). Runtime role flow_app (RLS).",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg://flow:flow@localhost:5432/flow",
        description="Sync URL (psycopg) for Alembic. Owner role flow.",
    )

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth / JWT. No default for the secret: it must be provided via
    # FLOW_JWT_SECRET (fail-closed). In dev it comes from .env.
    # >=32 bytes: RFC 7518 requirement for HMAC-SHA256.
    jwt_secret: str = Field(min_length=32, description="JWT signing secret.")
    jwt_alg: str = "HS256"
    jwt_ttl_seconds: int = 3600

    # App-level envelope for opaque secrets (OAuth tokens, IMAP
    # passwords; ADR-0006). Fernet key = urlsafe-b64 of 32 bytes (44
    # chars). No default: fail-closed, provided via FLOW_SECRET_KEY.
    secret_key: str = Field(
        min_length=44,
        description="Fernet key for the opaque-secret envelope.",
    )

    # Memory embeddings (docs/adr/0005). Fixed at the DDL level
    # (migration 0010); re-embedding to a new dim = a new column.
    embed_dim: int = 384

    # App
    env: str = "dev"
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings as a cached singleton."""
    return Settings()
