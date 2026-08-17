"""Fixed-window cap on what an unauthenticated caller can make us WRITE.

The connector ingress is public by construction: a payment provider posts to it
with no bearer, and the authority is the HMAC over the raw body. That part needs
no cap -- a caller without the signing secret cannot get a single event
ingested, and never could.

What it does need a cap on is the cost of being wrong. Every refused delivery
appends a row to ``payment_webhook_deliveries``, which is exactly the property
that makes the refusal auditable, and exactly the property an attacker who
learns a connector URL could use to grow that table without bound. The URL is
not guessable (a v4 UUID, 122 bits) but it is not a secret either: it is copied
into a provider dashboard, pasted in chats, read off screens.

So the bucket counts REFUSALS, not requests. A correctly signed flood is by
definition from someone holding the signing secret -- the provider -- so
legitimate traffic is never throttled no matter how it bursts. Past the budget
the refusal itself is unchanged -- the caller is turned away with the same 401
and learns nothing about a limit existing -- and only the ledger append stops.
The ledger keeps the first N refusals of the window, which is what an operator
would read anyway, and the attacker's marginal cost of another request drops to
one UPDATE.

One row per connector, maintained by the same atomic upsert shape as
``issuer_key_rate_limit`` (ADR-0045, migration 0078): the check has to be a
single statement, because the whole point is to be cheaper than the work it
replaces.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from mycelium_core.models.base import Base


class PaymentConnectorRefusalBucket(Base):
    __tablename__ = "payment_connector_refusals"

    connector_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payment_connectors.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: Denormalised from the connector so the bucket can be swept per tenant and
    #: read under RLS without a join back to a table the ingress resolves with
    #: no tenant context.
    org_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    window_start: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False)


__all__ = ["PaymentConnectorRefusalBucket"]
