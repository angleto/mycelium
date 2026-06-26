"""ADR-0036 event bus: the ``event_outbox`` row.

Authoritative record of a read/propose/commit/reject/snapshot event,
written in the same transaction as the originating mutation. The
``propose`` row is later UPDATEd with ``applied_at`` / ``applied_state``
when the adjudicator decides it, so this table is NOT append-only (unlike
``classification_feedback`` / ``activity_log``); RLS still pins each row
to its org. ``parent_event_id`` chains propose -> commit/reject so the
verdict is itself an event.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class EventOutbox(UUIDPKMixin, OrgScopedMixin, Base):
    __tablename__ = "event_outbox"

    # The acting subject (identity / user id). Not an FK: the row is an
    # audit event that must survive the actor's deletion.
    actor_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # Coarse actor class (human | agent | system); CHECK at the DB level.
    actor_kind: Mapped[str] = mapped_column(Text(), nullable=False)
    # read | propose | commit | reject | snapshot; CHECK at the DB level.
    kind: Mapped[str] = mapped_column(Text(), nullable=False)
    # Target node descriptor (nullable: a snapshot event is graph-wide).
    node_kind: Mapped[str | None] = mapped_column(Text(), nullable=True)
    node_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # propose -> commit/reject audit chain (self-reference; SET NULL on parent delete).
    parent_event_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    # Producer-supplied dedupe key; the bus dedupes inside a 24h window.
    idempotency_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Set when the adjudicator decides a propose (NULL until then).
    applied_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # committed | rejected | merged (NULL until decided); CHECK at the DB level.
    applied_state: Mapped[str | None] = mapped_column(Text(), nullable=True)
