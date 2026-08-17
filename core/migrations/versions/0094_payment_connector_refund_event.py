"""One refund event per connector, so one refund cannot become two credit notes.

ADR-0051 already admits exactly one emission trigger per connector, because
Stripe announces one payment several times (an ``invoice.paid`` is also a
``charge.succeeded``) and honouring a set of them would double-invoice. The
reversal side had the same shape and no such guard.

Stripe announces one refund twice: ``refund.created`` and ``charge.refunded``.
They deduplicate against each other only while the charge payload carries
expanded ``refunds.data``, because both then key the claim on the refund id.
Recent API versions stopped expanding that list, and the ``charge.refunded``
mapper cannot invent an id it was not sent: it falls back to a key derived from
the charge, which collides with nothing. An operator who ticks both boxes in the
provider's event picker -- easy to do, and invisible until it happens -- would
file TWO TD04 for one refund.

So the choice becomes configuration, mirroring ``emission_event``: the connector
honours ``refund_event`` and ignores the other announcement, recording it in the
ingress ledger with a reason rather than silently. ``refund.created`` is the
default (it always carries the refund id, and fires once per partial);
``charge.refunded`` stays available for endpoints that predate the newer event.

Revision ID: 0094
Revises: 0093
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0094"
down_revision: str | None = "0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The doubled prefix is what the database really has: see the note in 0093.
#: A new constraint added here has to be spelled the way the model's naming
#: convention will render it, or the metadata-vs-database check reports drift.
_CK_REFUND_EVENT = "ck_payment_connectors_ck_payment_connectors_refund_event"

_REFUND_EVENTS = "'refund.created','charge.refunded'"


def upgrade() -> None:
    op.add_column(
        "payment_connectors",
        sa.Column(
            "refund_event",
            sa.String(length=40),
            nullable=False,
            server_default="refund.created",
        ),
    )
    op.execute(
        f"ALTER TABLE payment_connectors ADD CONSTRAINT {_CK_REFUND_EVENT} "
        f"CHECK (refund_event IN ({_REFUND_EVENTS}))"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE payment_connectors DROP CONSTRAINT {_CK_REFUND_EVENT}")
    op.drop_column("payment_connectors", "refund_event")
