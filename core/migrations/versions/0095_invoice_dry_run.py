"""Mark a document as not-sent BECAUSE it was shadowed, not for some other reason.

Shadow mode (0093) records the run on the event: ``payment_connector_events``
knows it was a dry run and keeps the generated XML. The document it produced
knows nothing. It is a draft, archived out of the active list, and therefore
indistinguishable from every other archived draft -- work in progress, a
document waiting for review, one rejected by SdI and being redone.

That is the wrong place for the distinction to be missing. The whole point of a
parallel run is to answer "would this have been correct?" while an incumbent
provider files the real documents, and the operator's question at the end is
"which of these was held back only because we were shadowing?" Answering it by
joining through the ingress ledger works but is not legible on the surface
where invoices are actually read, and an orphaned shadow (its claim discarded)
would read as an ordinary draft.

So the document carries the reason itself. The flag is cleared by
``promote_dry_run``, which turns a shadow into a real, sendable draft and moves
its claim rows out of the shadow universe in the same transaction -- the two can
never disagree, and a promoted document is refused if a live document already
covers the same payment.

Revision ID: 0095
Revises: 0094
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # Partial: shadow documents are a small, transient minority of the table
    # (they exist only during a parallel run), and the only query that wants
    # them asks for exactly this predicate.
    op.create_index(
        "ix_invoices_dry_run",
        "invoices",
        ["org_id", "issuer_profile_id"],
        postgresql_where=sa.text("dry_run"),
    )
    # Backfill what the ledger already knows, so a connector that has been
    # shadowing since 0093 does not leave its existing documents unlabelled --
    # they are exactly the ones an operator is about to have to classify.
    op.execute(
        "UPDATE invoices SET dry_run = true WHERE id IN ("
        "  SELECT invoice_id FROM payment_object_links WHERE dry_run"
        ")"
    )


def downgrade() -> None:
    op.drop_index("ix_invoices_dry_run", table_name="invoices")
    op.drop_column("invoices", "dry_run")
