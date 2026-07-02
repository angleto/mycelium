"""Idempotency claims for the state-changing public Invoice REST endpoints.

A client ``Idempotency-Key`` header on compose / transmit / credit-note is
claimed atomically (insert-on-conflict-do-nothing in the SAME transaction as the
mutation), so a network retry never files a second fiscal document. The claim is
unique on ``(issuer_profile_id, endpoint, idempotency_key)`` -- scoped to the
ISSUER, not the key, so a key rotation or re-mint mid-retry keeps the dedupe.

``request_hash`` (sha256 of the canonical request) lets a re-use of the same key
with a different body be a 409/422 rather than silently returning the first
result. ``response_snapshot`` is filled after the mutation commits (NULL between
claim and completion). Rows carry ``created_at`` for a TTL purge and are reachable
by the GDPR-erase path (the snapshot holds cessionario PII). FORCE RLS, like
``event_outbox``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import (
    Base,
    OrgScopedMixin,
    TimestampMixin,
    UUIDPKMixin,
)


class ApiIdempotency(UUIDPKMixin, OrgScopedMixin, TimestampMixin, Base):
    __tablename__ = "api_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "issuer_profile_id",
            "endpoint",
            "idempotency_key",
            name="uq_api_idempotency_claim",
        ),
        Index("ix_api_idempotency_created_at", "created_at"),
    )

    issuer_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # sha256 of the canonical request body; guards against key reuse with a
    # different payload.
    request_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Filled after the mutation commits; NULL between claim and completion.
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
    )


__all__ = ["ApiIdempotency"]
