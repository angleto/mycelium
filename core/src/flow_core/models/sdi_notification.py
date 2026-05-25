"""SdI notification audit log (active + receiver cycles).

Append-only tables that record every SdI notification we exchange. Each row
is the raw signed XML plus a structured ``payload`` JSONB for fast queries
(error codes for NS, EsitoCommittente blob for NE, etc.). The dedupe unique
index lets the ingest service swallow SdI retries idempotently.

These tables sit alongside the denormalized verdict columns on ``invoices``
and ``received_invoices`` (the latest derived state for fast filtering); a
notification ingest writes both the audit row and the verdict update in the
same transaction.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from flow_core.models.base import Base, OrgScopedMixin, UUIDPKMixin


class InvoiceNotification(UUIDPKMixin, OrgScopedMixin, Base):
    """A notification received on the active cycle for a transmitted
    ``Invoice`` (RC, MC, NS, AT, NE, DT). Insert is idempotent on the
    ``(invoice_id, kind, message_id)`` unique index, so an SdI retry with
    the same MessageId no-ops."""

    __tablename__ = "invoice_notifications"
    __table_args__ = (
        Index(
            "uq_invoice_notifications_dedupe",
            "invoice_id",
            "kind",
            "message_id",
            unique=True,
        ),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # One of: RC / MC / NS / AT / NE / DT (enforced by CHECK constraint on the
    # column; not a native PG enum to keep additions cheap).
    kind: Mapped[str] = mapped_column(String(2), nullable=False)
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    nome_file: Mapped[str | None] = mapped_column(String(120), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(14), nullable=True)
    raw_xml: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ReceivedInvoiceNotification(UUIDPKMixin, OrgScopedMixin, Base):
    """A notification on the receiver cycle for a ``ReceivedInvoice``. Kinds:
    MT (delivery metadata), SE (our EC was scartato), DT (15-day timeout),
    EC (our outbound esito-committente, ``direction='out'``). The unique
    dedupe key spans direction so an inbound DT and a re-sent EC do not
    collide."""

    __tablename__ = "received_invoice_notifications"
    __table_args__ = (
        Index(
            "uq_received_invoice_notifications_dedupe",
            "received_invoice_id",
            "kind",
            "direction",
            "message_id",
            unique=True,
        ),
    )

    received_invoice_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("received_invoices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(2), nullable=False)
    # 'in' = SdI to us; 'out' = we built and sent (EC outbound).
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    nome_file: Mapped[str | None] = mapped_column(String(120), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(14), nullable=True)
    raw_xml: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
