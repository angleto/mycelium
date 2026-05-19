"""Application configuration.

All variables are prefixed ``FLOW_`` (12-factor, see
docs/non-functional-requirements.md). No hardcoded secrets.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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

    # Max size of a single note/task attachment. Stored as BYTEA in the
    # DB (no object store; co-tenant deploy), so the cap is deliberately
    # conservative. Enforced server-side in the attachments service
    # before the bytes are persisted. Override via FLOW_ATTACHMENT_MAX_BYTES.
    attachment_max_bytes: int = 10 * 1024 * 1024

    # Pluggable attachment storage backend (mirrors the LLM/embedder
    # seam). "pg" (default) keeps today's behaviour exactly: bytes live
    # in the attachments.data BYTEA column, atomic with the row, no
    # external dependency. "s3" offloads the bytes to an S3-compatible
    # object store (Scaleway Object Storage) and keeps only metadata +
    # a storage_key in the DB. Selected via FLOW_ATTACHMENT_STORE.
    attachment_store: Literal["pg", "s3"] = "pg"
    # S3 (Scaleway is S3-compatible) target. Only consulted/required when
    # attachment_store == "s3" (a model validator fails closed below so a
    # half-configured prod is rejected at startup; "pg" needs none).
    attachment_s3_endpoint_url: str = ""
    attachment_s3_region: str = ""
    attachment_s3_bucket: str = ""
    attachment_s3_access_key_id: str = ""
    attachment_s3_secret_access_key: str = ""
    # Optional key namespace inside the bucket (e.g. "flow/attachments").
    attachment_s3_prefix: str = ""

    # Public self-service signup. Default True so OSS self-hosters and
    # the existing test-suite are unaffected; set FLOW_ALLOW_SIGNUP=false
    # for a single-user prod (the admin is provisioned out-of-band by
    # `python -m flow_core.bootstrap_admin`, which calls the service
    # layer, not the gated HTTP endpoint).
    allow_signup: bool = True

    # Auth hardening (W1b, ported from bitvision_phoenix; ADR-0024).
    require_email_verification: bool = False
    email_verification_ttl_seconds: int = 86400
    password_reset_ttl_minutes: int = 30
    # TOTP MFA.
    mfa_issuer: str = "Flow"
    require_mfa_for_admin: bool = False
    # Login lockout (failed attempts within the window lock the account).
    login_max_failures: int = 5
    login_lockout_seconds: int = 900
    # Base URL the verification/reset links point at (the SPA).
    frontend_base_url: str = "http://localhost:5173"

    # CORS. Comma-separated allowed origins for the browser SPA when it
    # is served from a different origin than the API (production splits
    # flow.leto.blue → SPA and api.flow.leto.blue → API). Empty disables
    # the middleware (same-origin dev/proxy needs no CORS).
    cors_origins: str = "http://localhost:5173"

    # Closed-loop dispatch worker (docs/adr/0025 P5). The periodic tick
    # interval, in seconds. Modest by default (do not hammer the
    # scheduler); per-workspace and exception-isolated. Configurable via
    # FLOW_DISPATCH_LOOP_INTERVAL_SECONDS like the other worker knobs.
    dispatch_loop_interval_seconds: int = 60

    # App
    env: str = "dev"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate_attachment_store(self) -> Settings:
        """Fail closed: if the S3 backend is selected, every credential /
        target must be present. A half-configured prod is rejected at
        startup rather than failing on the first upload (same spirit as
        the no-default jwt_secret / secret_key fields)."""
        if self.attachment_store == "s3":
            missing = [
                name
                for name, value in (
                    ("FLOW_ATTACHMENT_S3_ENDPOINT_URL", self.attachment_s3_endpoint_url),
                    ("FLOW_ATTACHMENT_S3_REGION", self.attachment_s3_region),
                    ("FLOW_ATTACHMENT_S3_BUCKET", self.attachment_s3_bucket),
                    ("FLOW_ATTACHMENT_S3_ACCESS_KEY_ID", self.attachment_s3_access_key_id),
                    (
                        "FLOW_ATTACHMENT_S3_SECRET_ACCESS_KEY",
                        self.attachment_s3_secret_access_key,
                    ),
                )
                if not value
            ]
            if missing:
                raise ValueError("attachment_store='s3' requires: " + ", ".join(sorted(missing)))
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings as a cached singleton."""
    return Settings()
