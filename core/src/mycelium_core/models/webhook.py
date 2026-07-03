"""Signed outbound webhooks on invoice state changes (task 2c23e955, ADR-0047).

Two tables, both org-scoped with FORCE row-level security:

- :class:`WebhookEndpoint` -- a per-issuer-profile subscription. Holds the
  signing secret as a REVERSIBLE Fernet envelope (the server recomputes the
  HMAC on every delivery, so it must recover the plaintext; unlike an inbound
  bearer credential this is NOT a one-way hash). Rotation keeps a previous
  ciphertext valid until ``previous_secret_expires_at``.
- :class:`WebhookDelivery` -- one row per (event, endpoint), a transactional
  outbox. The ``payload_snapshot`` is FROZEN at emit time so a later invoice
  mutation never changes what is delivered, and the row ``id`` is the stable
  ``X-Webhook-Id`` the receiver replay-guards on. Delivered by a decoupled
  worker with an at-least-once lease (``status='delivering'`` +
  ``last_attempt_at``).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
    VersionMixin,
)


class WebhookEndpoint(UUIDPKMixin, OrgScopedMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        CheckConstraint(
            "length(name) >= 1 AND length(name) <= 120", name="ck_webhook_endpoints_name_len"
        ),
        Index("ix_webhook_endpoints_issuer_profile_id", "issuer_profile_id"),
        # One active endpoint per (issuer, url): a duplicate would fan out two
        # deliveries with different X-Webhook-Ids the receiver can't collapse.
        Index(
            "uq_webhook_endpoints_active_url",
            "issuer_profile_id",
            "url",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    issuer_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    # Fernet ciphertext of the HMAC signing secret (reversible; keyed by
    # MYCELIUM_SECRET_KEY). Shown to the owner exactly once at create / rotate.
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    previous_secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_secret_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Subscribed event types; empty = all. Whitelisted at the service.
    event_types: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'::text[]")
    )
    active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WebhookDelivery(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # The emit is INSERT ... ON CONFLICT DO NOTHING on this key, so an SdI
        # redelivery / lost-ACK reconcile / double codepath never double-fires.
        UniqueConstraint("endpoint_id", "dedupe_key", name="uq_webhook_deliveries_dedupe"),
        CheckConstraint(
            "status IN ('pending','delivering','delivered','failed','dead')",
            name="ck_webhook_deliveries_status",
        ),
        # The drain query: rows due for a send attempt.
        Index(
            "ix_webhook_deliveries_due",
            "next_attempt_at",
            postgresql_where=text("status IN ('pending','failed')"),
        ),
        # The lease-reclaim query: stuck 'delivering' rows.
        Index(
            "ix_webhook_deliveries_delivering",
            "last_attempt_at",
            postgresql_where=text("status = 'delivering'"),
        ),
        Index("ix_webhook_deliveries_invoice", "invoice_id"),
        Index("ix_webhook_deliveries_created_at", "created_at"),
    )

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    issuer_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # SET NULL, not CASCADE: an invoice is a fiscal record and is not
    # hard-deleted (taxonomy.purge_client refuses a client with invoices), so
    # this is only a theoretical safety net; the snapshot stays erasable.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_attempt_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_excerpt: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
