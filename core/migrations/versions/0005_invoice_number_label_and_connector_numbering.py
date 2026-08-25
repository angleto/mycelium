"""A document can carry a number this system did not mint.

Two columns for one decision. ``invoices.number_label`` holds the emitted
``<Numero>`` verbatim when the identity comes from outside our counters, and
``payment_connectors.numbering`` says who assigns it.

The label is a STRING and not a parsed integer on purpose: Stripe numbers an
invoice "4D41B1BD-0046", and mapping that onto ``series`` + ``number`` emits
"4D41B1BD-46" -- the zero padding is lost and the number on the fiscal document
stops matching the receipt the customer is holding, which is the entire reason
to adopt the provider's number in the first place.

Its uniqueness is per ISSUER only, not per (issuer, series, year) like the
counter-minted one: a provider's sequence is global to the provider account and
carries no year of ours, so scoping it by year would let the same number in
twice. NULL rows are unconstrained -- Postgres treats NULLs as distinct -- so
every existing document is untouched by it.

``numbering`` is backfilled from what each connector already expresses: a
connector with an explicit ``series`` was already running one sequence for the
whole connector, and one without was letting ``create_draft`` derive a sezionale
per counterpart. Reading that from the data rather than defaulting everything to
``client`` is what keeps the change invisible to a running deployment.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # String20Type in the tracciato: 1..20 Basic Latin characters.
    op.add_column("invoices", sa.Column("number_label", sa.String(length=20), nullable=True))
    op.create_unique_constraint(
        "uq_invoices_issuer_label", "invoices", ["issuer_profile_id", "number_label"]
    )
    op.add_column(
        "payment_connectors",
        sa.Column("numbering", sa.String(length=12), nullable=False, server_default="client"),
    )
    op.execute(
        "UPDATE payment_connectors SET numbering = 'series' "
        "WHERE series IS NOT NULL AND series <> ''"
    )
    op.create_check_constraint(
        "ck_payment_connectors_numbering",
        "payment_connectors",
        "numbering IN ('client', 'series', 'provider')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_payment_connectors_numbering", "payment_connectors", type_="check")
    op.drop_column("payment_connectors", "numbering")
    op.drop_constraint("uq_invoices_issuer_label", "invoices", type_="unique")
    op.drop_column("invoices", "number_label")
