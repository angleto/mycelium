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
    # Refresh-token TTL (default 90d). Rotated on every /auth/refresh,
    # so a session that's actively used renews indefinitely; only an
    # idle gap beyond this kicks the user back to /login.
    refresh_token_ttl_seconds: int = 90 * 24 * 3600

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

    # System (transactional) mailer transport (W1b; ADR-0024). Pluggable
    # like attachment_store: empty host (default) = the LogMailer, so
    # OSS self-host + dev + the whole test-suite are unaffected (the
    # verification/reset link is in the body and is logged). Setting
    # FLOW_SMTP_HOST (+ FLOW_SMTP_FROM) switches to the real SmtpMailer
    # (stdlib smtplib over STARTTLS for Scaleway TEM, port 587). The
    # username/password are secret (k8s flow-smtp Secret) and may be
    # legitimately empty for an unauthenticated relay, so they are NOT
    # required; only host+from gate the SMTP transport (validated
    # fail-closed below, same spirit as attachment_store='s3').
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

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
    # flow.xeno.garden → SPA and api.flow.xeno.garden → API). Empty disables
    # the middleware (same-origin dev/proxy needs no CORS).
    cors_origins: str = "http://localhost:5173"

    # Google OAuth 2.0 (Gmail + Google Calendar; epic #125 P1). All
    # empty by default so OSS self-hosters / dev / the test-suite are
    # unaffected; ``google_configured`` is True iff all three are set
    # and the router refuses to start an OAuth flow otherwise (same
    # fail-closed spirit as smtp_configured). The redirect URI must
    # match the one registered in the Google Cloud console exactly; it
    # points at our public callback (``/oauth/google/callback``).
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    # Tick interval for the periodic Google Calendar sync worker (epic
    # #125 P1). Modest by default (do not hammer Google's API);
    # per-subscription and exception-isolated, like the dispatch loop.
    google_calendar_sync_interval_seconds: int = 300

    # Closed-loop dispatch worker (docs/adr/0025 P5). The periodic tick
    # interval, in seconds. Modest by default (do not hammer the
    # scheduler); per-workspace and exception-isolated. Configurable via
    # FLOW_DISPATCH_LOOP_INTERVAL_SECONDS like the other worker knobs.
    dispatch_loop_interval_seconds: int = 60

    # Reminders + notification-dispatch worker. One periodic tick scans
    # due reminders into pending Notifications (idempotent by
    # dedupe_key) and then dispatches all pending notifications through
    # the configured sender per channel pref. Per-workspace and
    # exception-isolated like the dispatch loop. Configurable via
    # FLOW_REMINDERS_LOOP_INTERVAL_SECONDS. 60s is the floor for
    # minute-precision reminders on appointment tasks.
    reminders_loop_interval_seconds: int = 60

    # Task-search embedding backfill worker. Re-embeds task blobs whose
    # initial write timed out (model_id='none'); race-protected via the
    # pointer ``content_hash``. Per-workspace + exception-isolated like
    # the other loops. 60s is a low-cost default: the population is
    # bounded (only timed-out writes) and the worker quickly drains.
    task_search_backfill_interval_seconds: int = 60

    # Embedding model migration (task 1d081395). The dual-column
    # pattern in migration 0009 lets us roll a new embedder (bge-m3
    # 1024d default) without downtime: ``FLOW_EMBED_MODEL`` is the v1
    # legacy model (left as is for backward-compat with installed
    # deployments), ``FLOW_EMBED_MODEL_V2`` is the new target. When
    # set, new writes populate ``embedding_v2`` and the migration
    # worker backfills v2 for legacy rows. ``embed_dim_v2`` must match
    # the model's actual dim (pgvector is strict). Leaving v2 unset
    # keeps the system on v1 only (legacy single-model behaviour).
    embed_model: str = "intfloat/multilingual-e5-small"
    embed_model_v2: str = ""
    embed_dim_v2: int = 1024
    # Embedding migration worker (sweep-rate per workspace). Like the
    # task-search backfill: per-workspace + exception-isolated, modest
    # default so a large workspace drains without saturating the API.
    embedding_migration_interval_seconds: int = 60

    # Cross-encoder reranker (task 27579d6a). Opt-in second-stage
    # scorer that re-orders the top-K from RRF using a cross-encoder
    # model (sees query+doc joined). Off by default because the local
    # model is ~1GB resident and adds latency to interactive search;
    # production toggles it on selectively. The per-call ``rerank=true``
    # body flag also enables it for a single search regardless of the
    # env default. The model id is the sentence-transformers
    # ``CrossEncoder`` identifier; the default targets
    # ``bge-reranker-v2-m3`` which is multilingual and pairs naturally
    # with bge-m3 dense embeddings.
    reranker_enabled: bool = False
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # Gate thresholds: skip the reranker on queries too short to
    # discriminate (one-word queries leave the cross-encoder little to
    # work with) or on candidate sets too small to be worth the cost.
    reranker_min_query_tokens: int = 3
    reranker_min_candidates: int = 5

    # Recovery-history retention worker (entity_revision). The sweep
    # has two parts: a coarsening pass that keeps 1 revision/day past
    # ``revisions_retain_full_days`` and 1 revision/week past
    # ``revisions_coarse_to_weekly_days``, and a hard-delete of
    # soft-deleted task/note rows older than
    # ``revisions_hard_delete_after_days`` (the cascade trigger then
    # purges their revisions). Cadence is daily by default because
    # the time-bucket thresholds are coarse; can be sped up in tests.
    revisions_retention_interval_seconds: int = 86_400
    revisions_retain_full_days: int = 30
    revisions_coarse_to_weekly_days: int = 365
    revisions_hard_delete_after_days: int = 90

    # Telegram bot integration (epic #125 P2). Empty defaults so OSS
    # self-host + tests are unaffected: the link router refuses to mint
    # codes when the bot is not configured, the webhook 404s on an empty
    # secret, and the Telegram NotificationSender records a failure
    # instead of trying to reach api.telegram.org. The HTTP client
    # Protocol allows a fake-in-test injection (same seam pattern as the
    # email connector / LLM provider). The bot token + webhook secret
    # are deploy secrets (env / k8s Secret); the bot username is public
    # (built into the t.me deep-link). The webhook base URL falls back
    # to frontend_base_url when not set, since the SPA + API typically
    # share a host in the OSS deploy; production override points at the
    # API origin.
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_base_url: str = ""
    # Network timeout for Telegram Bot API calls (seconds). Short on
    # purpose: the webhook handler must reply quickly or Telegram retries.
    telegram_http_timeout_seconds: float = 10.0

    # Conversational assistant (ADR-0026). When enabled, a free-text
    # Telegram message is handled by an in-process LLM agent that uses
    # Flow's tools (read/scoped-write on notes/tasks) and replies. Off by
    # default: OSS / dev / CI keep the safe "free-text -> hint" behavior
    # unless a model is configured. Spend follows the configured model
    # (free local Ollama costs nothing; a premium model debits credits via
    # the existing metering); ``assistant_credit_budget`` caps per-turn
    # spend (<= 0 means no cap) and ``assistant_max_steps`` bounds the
    # tool/think loop regardless of the script.
    assistant_enabled: bool = False
    assistant_max_steps: int = 8
    assistant_credit_budget: float = 0.0
    # Worker poll interval for the assistant job queue (ADR-0026 P3).
    assistant_loop_interval_seconds: int = 5

    # SdI electronic-invoice transmission (ADR-0011, FR-9). Default
    # "manual_export": Flow builds downloadable XML and the tenant submits
    # it (no SdI transit; AdE conservation not covered). "sdicoop":
    # transmit through Flow's single accredited channel as intermediary --
    # the tenant's identity stays in the FatturaPA payload, Flow's goes in
    # TerzoIntermediarioOSoggettoEmittente (requires a per-issuer
    # SdiMandate). The intermediary identity + the mutual-TLS SOAP
    # transport below are required only when "sdicoop" is selected
    # (validated fail-closed, same spirit as smtp/s3). id_codice is the
    # accredited channel holder's P.IVA; cert/key/ca are PEM file paths
    # (deploy secrets).
    sdi_channel: Literal["manual_export", "sdicoop"] = "manual_export"
    sdi_intermediary_id_paese: str = "IT"
    sdi_intermediary_id_codice: str = ""
    sdi_intermediary_denominazione: str = ""
    sdi_endpoint_url: str = ""
    sdi_client_cert: str = ""
    sdi_client_key: str = ""
    sdi_ca_bundle: str = ""
    # EsitoCommittente outbound (ADR-0011 v1.1): the NotificaEsitoCommittente
    # we generate as buyer carries an XMLDSig enveloped signature. The
    # signing key / cert are PEM bytes loaded from the deploy secret store;
    # tests inject ephemeral self-signed material at runtime. Empty (the
    # default) means "no EC outbound is wired"; the service raises on call.
    sdi_ec_signing_key_pem: str = ""
    sdi_ec_signing_cert_pem: str = ""

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

    @model_validator(mode="after")
    def _validate_smtp(self) -> Settings:
        """Fail closed: configuring SMTP means configuring it fully. An
        empty FLOW_SMTP_HOST is the safe dev/OSS default (LogMailer, no
        requirements). A non-empty host means the real SmtpMailer, which
        needs an envelope/From identity, so FLOW_SMTP_FROM is required;
        a half-configured prod is rejected at startup rather than
        emitting mail with no From. Username/password may be empty (an
        unauthenticated relay is valid), so they are not required here."""
        if self.smtp_host and not self.smtp_from:
            raise ValueError("FLOW_SMTP_HOST is set but FLOW_SMTP_FROM is required to enable SMTP")
        return self

    @model_validator(mode="after")
    def _validate_sdi(self) -> Settings:
        """Fail closed: selecting the SdICoop channel means configuring it
        fully (intermediary identity + mutual-TLS transport). The CA bundle
        is optional (the system trust store is acceptable for SdI's cert)."""
        if self.sdi_channel == "sdicoop":
            missing = [
                name
                for name, value in (
                    ("FLOW_SDI_INTERMEDIARY_ID_CODICE", self.sdi_intermediary_id_codice),
                    ("FLOW_SDI_INTERMEDIARY_DENOMINAZIONE", self.sdi_intermediary_denominazione),
                    ("FLOW_SDI_ENDPOINT_URL", self.sdi_endpoint_url),
                    ("FLOW_SDI_CLIENT_CERT", self.sdi_client_cert),
                    ("FLOW_SDI_CLIENT_KEY", self.sdi_client_key),
                )
                if not value
            ]
            if missing:
                raise ValueError("sdi_channel='sdicoop' requires: " + ", ".join(sorted(missing)))
        return self

    @property
    def google_configured(self) -> bool:
        """Google OAuth is active iff client_id, client_secret and
        redirect_uri are all set. Same fail-closed spirit as
        smtp_configured: the OAuth router rejects flows when False."""
        return bool(
            self.google_client_id and self.google_client_secret and self.google_redirect_uri
        )

    @property
    def telegram_configured(self) -> bool:
        """Telegram bot is active iff bot token, bot username and the
        webhook secret are all set. Same fail-closed spirit as
        smtp_configured / google_configured: the router refuses to mint
        link codes and the webhook 404s when False. The username is
        required because the deep-link URL (``https://t.me/<bot>?start=
        <code>``) cannot be built without it."""
        return bool(
            self.telegram_bot_token and self.telegram_bot_username and self.telegram_webhook_secret
        )

    @property
    def telegram_webhook_url_base(self) -> str:
        """Resolved public base URL the bot webhook is exposed at: the
        explicit override when set, otherwise the SPA's base URL (the
        co-host default in OSS deploys). Empty when neither is set."""
        return self.telegram_webhook_base_url or self.frontend_base_url

    @property
    def smtp_configured(self) -> bool:
        """SMTP transport is active iff host and From are both set
        (the validator above guarantees from is present when host is)."""
        return bool(self.smtp_host and self.smtp_from)

    @property
    def sdicoop_active(self) -> bool:
        """True iff invoices are transmitted through Flow's accredited
        SdICoop channel (Flow as intermediary). False = manual export."""
        return self.sdi_channel == "sdicoop"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings as a cached singleton."""
    return Settings()
