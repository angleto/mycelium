"""Record the payments a connector received but skipped.

Until this release a payment connector recorded a payment only on a document
that was already filed: a dry-run shadow and a draft were skipped, and a skip
was not a deferral -- nothing looked again. Every shadow therefore read
"unpaid" although the provider had collected the money (measured 2026-10-16:
174 shadows of the hahnbanach connector, every one built from a Stripe
``invoice.paid``), and a shadow promoted and filed, or a draft composed in
draft mode and transmitted by hand, would have come out unpaid too. The
service now records the payment whatever the document's state; this records
it on the documents written before.

WHICH ROWS. An unpaid invoice that a connector composed from its emission
event, processed to ``done``, on a connector with payment sync enabled. Every
emission intent the adapters build is a payment (``invoice.paid``,
``payment_intent.succeeded``, ``checkout.session.completed`` all carry
``paid=True``), so the processed event IS the evidence of the payment.

NO ANNOUNCEMENT. ``invoice.payment_recorded`` is not enqueued here. The
service announces a payment only about a filed document, and a filed document
in this set could only be one transmitted by hand after the connector had
skipped its payment, and production holds none (measured 2026-10-16: the
update matches 174 rows, every one an archived dry-run draft).

Idempotent: a recorded row is paid and no longer matches.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECORD_SQL = """
UPDATE invoices AS i
   SET payment_status = 'paid',
       version = i.version + 1,
       updated_at = now()
 WHERE i.payment_status = 'unpaid'
   AND EXISTS (
         SELECT 1
           FROM payment_connector_events AS e
           JOIN payment_connectors AS c ON c.id = e.connector_id
          WHERE e.invoice_id = i.id
            AND e.event_type = c.emission_event
            AND e.status = 'done'
            AND c.payment_sync_enabled
       )
"""


def upgrade() -> None:
    op.execute(RECORD_SQL)


def downgrade() -> None:
    # Deliberately empty. The provider collected every one of these payments;
    # marking them unpaid again would restore the defect, not the data.
    pass
