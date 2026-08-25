"""Record how a connector's customer actually pays.

FatturaPA wants a ModalitaPagamento and the system had no way to know one: an
``invoice.paid`` payload carries the charge as a bare id, and a webhook body
cannot be expanded, so the code was emitted from connector configuration -- an
assumption about the account rather than a fact about the money. The charge
object states it (``payment_method_details.type``) and arrives as its own
``charge.succeeded`` event, which is already subscribed.

Recorded per (connector, provider customer) rather than read at composition,
because the fact usually arrives too late to read then: the charge is created
seconds after the invoice is marked paid, Stripe does not guarantee delivery
order, and in transmit mode the document is composed and filed inside the single
invoice.paid event. The instrument is a property of the customer far more often
than of the individual payment, so the previous cycle answers for this one.

Not on ``client_profile``: that record is the org's own anagrafica and feeds
hand-written invoices too, so a connector's observation there would leak onto
documents the connector never composed.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_customer_links",
        sa.Column("observed_method_type", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payment_customer_links", "observed_method_type")
