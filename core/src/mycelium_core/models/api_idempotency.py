"""Idempotency claims for state-changing endpoints that must not repeat.

A client ``Idempotency-Key`` header on compose / transmit / credit-note is
claimed atomically (insert-on-conflict-do-nothing in the SAME transaction as the
mutation), so a network retry never files a second fiscal document. The claim is
unique on ``(issuer_profile_id, endpoint, idempotency_key)`` -- scoped to the
ISSUER, not the key, so a key rotation or re-mint mid-retry keeps the dedupe.

A second principal was added for the capture endpoints (``POST /tasks``,
``POST /notes``), where the caller is a person in a workspace rather than an
issuer: ``(org_id, actor_id, endpoint, idempotency_key)``. Exactly one of the
two is set per row, enforced by a CHECK, and each is served by a PARTIAL
unique index -- NULLs are distinct in a unique index, so one constraint over a
nullable ``issuer_profile_id`` would make every actor claim unique by accident
and deduplicate nothing. ``org_id`` is in the actor key and not in the issuer
key because an issuer profile already implies one workspace, while one person
retrying in two workspaces must not have the second capture read as a replay
of the first.

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

from sqlalchemy import CheckConstraint, ForeignKey, Index, LargeBinary, String, text
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
        CheckConstraint(
            "num_nonnulls(issuer_profile_id, actor_id) = 1",
            name="ck_api_idempotency_one_principal",
        ),
        Index(
            "uq_api_idempotency_issuer_claim",
            "issuer_profile_id",
            "endpoint",
            "idempotency_key",
            unique=True,
            postgresql_where=text("issuer_profile_id IS NOT NULL"),
        ),
        Index(
            "uq_api_idempotency_actor_claim",
            "org_id",
            "actor_id",
            "endpoint",
            "idempotency_key",
            unique=True,
            postgresql_where=text("actor_id IS NOT NULL"),
        ),
        Index("ix_api_idempotency_created_at", "created_at"),
    )

    # Exactly one of the two is set. The issuer branch is the invoice API;
    # the actor branch is a person capturing into a workspace.
    issuer_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
        nullable=True,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
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
