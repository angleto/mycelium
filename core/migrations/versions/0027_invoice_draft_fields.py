"""Invoice draft fields: notes, payment IBAN, payment due date.

An invoice draft is fully editable until transmission (ADR-0009). Beyond
the line items it now carries operator-facing fields the issuer fills in
before emission: free ``notes``, the ``payment_iban`` to be paid to, and
a ``payment_due_date`` (FatturaPA ``DataScadenzaPagamento``). All three
are only mutable while ``draft`` (enforced in the service) and ride
along into the FatturaPA payload at transmit time.

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS notes text",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_iban varchar(34)",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_due_date date",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE invoices DROP COLUMN IF EXISTS payment_due_date",
    "ALTER TABLE invoices DROP COLUMN IF EXISTS payment_iban",
    "ALTER TABLE invoices DROP COLUMN IF EXISTS notes",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
