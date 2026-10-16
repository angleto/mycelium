"""Put back in the shadow universe the shadow documents that escaped it.

A payment connector in ``dry_run`` builds each document and validates it
against the XSD, then marks it ``dry_run`` and archives it. Until this release
the marking came AFTER the validation, and a document that failed it parked
its event without rolling the transaction back: the draft stayed, unflagged
and in the active list, where it reads as a real invoice awaiting payment.
Production held seven of them on 2026-10-16, and they were reported as
"Stripe-paid invoices we show unpaid". The service now marks before it
validates; this repairs the rows written before it did.

WHICH ROWS. A draft, not flagged ``dry_run``, whose every provider claim in
``payment_object_links`` is a shadow claim. That is exactly the escaped
shape and nothing else:

- a live emission (``invoice_mode`` draft or transmit) claims with
  ``dry_run = false``, so its drafts have at least one live link;
- ``promote_dry_run`` moves the claims to live together with the document,
  so a promoted shadow has live links and is left alone;
- a hand-written invoice has no links at all.

Measured on production before writing this (read-only, 2026-10-16): the
predicate matches 7 rows, the 7 events parked as ``dry_run_invalid_document``.

The ``version`` bump is what the service's own ``archive_invoice`` does, so
an editor holding the old version is refused rather than silently reverting
the change.

Idempotent: a repaired row is flagged and no longer matches.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESHADOW_SQL = """
UPDATE invoices AS i
   SET dry_run = true,
       is_archived = true,
       version = i.version + 1,
       updated_at = now()
 WHERE i.state = 'draft'
   AND NOT i.dry_run
   AND EXISTS (SELECT 1 FROM payment_object_links l WHERE l.invoice_id = i.id)
   AND NOT EXISTS (
         SELECT 1 FROM payment_object_links l WHERE l.invoice_id = i.id AND NOT l.dry_run
       )
"""


def upgrade() -> None:
    op.execute(RESHADOW_SQL)


def downgrade() -> None:
    # Deliberately empty. Un-flagging cannot be correct: this migration does
    # not record which rows it touched, and every row it touched was a shadow
    # document whose correct state is the one it now has.
    pass
