"""Reading a provider amount is not a yes/no question.

``amounts_include_vat`` was a boolean consulted whenever the payload carried no
explicit tax breakdown, and it defaulted to FALSE -- "the figure is net, add VAT
on top". For a payment connector that default is not conservative, it is wrong:
the figure is money that has already been collected, so it IS the document
total. A 25.00 charge with no tax breakdown became a 30.50 invoice.

That was reachable on live traffic. A Stripe account on the 2026-07-29 API
reports line tax under ``taxes``/``tax_behavior``; an adapter reading only the
older ``tax_amounts``/``inclusive`` sees no tax at all, falls through to this
switch, and inflates every line by the default rate.

So the setting becomes three-valued, and its job is narrowed to what it can
actually answer:

- ``auto`` (default): obey the payload when it states a tax behaviour -- it is
  stating a fact about money that moved -- and treat a SILENT payload as
  VAT-inclusive, because the amount is the total collected.
- ``gross`` / ``net``: force, ignoring the payload. A blunt instrument for a
  feed whose own tax flags are known to be wrong.

Existing rows are migrated to the MEANING they had, except for the default:
``true`` becomes ``gross`` (a deliberate choice, preserved), while ``false``
becomes ``auto`` rather than ``net``. False was the server default, so it marks
"never decided" far more often than "decided that amounts are net", and
carrying it forward would carry the bug forward with it.

Revision ID: 0098
Revises: 0097
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0098"
down_revision: str | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CK = "ck_payment_connectors_ck_payment_connectors_vat_pricing"


def upgrade() -> None:
    op.add_column(
        "payment_connectors",
        sa.Column("vat_pricing", sa.String(length=8), nullable=False, server_default="auto"),
    )
    op.execute("UPDATE payment_connectors SET vat_pricing = 'gross' WHERE amounts_include_vat")
    op.execute(
        f"ALTER TABLE payment_connectors ADD CONSTRAINT {_CK} "
        "CHECK (vat_pricing IN ('auto', 'gross', 'net'))"
    )
    op.drop_column("payment_connectors", "amounts_include_vat")


def downgrade() -> None:
    op.add_column(
        "payment_connectors",
        sa.Column(
            "amounts_include_vat",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # ``auto`` has no boolean equivalent; false is the closest surviving
    # behaviour and is what it was before this migration.
    op.execute(
        "UPDATE payment_connectors SET amounts_include_vat = true WHERE vat_pricing = 'gross'"
    )
    op.execute(f"ALTER TABLE payment_connectors DROP CONSTRAINT {_CK}")
    op.drop_column("payment_connectors", "vat_pricing")
