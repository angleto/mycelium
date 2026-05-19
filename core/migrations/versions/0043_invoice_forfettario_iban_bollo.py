"""Forfettario-correct invoicing: payment IBAN precedence + virtual bollo.

Three plain column adds, no data backfill semantics beyond the literal
defaults:

- ``issuer_profiles.default_iban``: the issuer's fallback payment IBAN,
  used when neither the invoice nor the client carries one. NULL by
  default (existing issuers had no such field; behaviour unchanged until
  one is set).
- ``client_profile.payment_iban``: a client-specific payment IBAN that
  overrides the issuer default. NULL by default. (The table is
  ``client_profile``, singular: it is the 1:1 satellite of a client
  tag, not a plural collection.)
- ``invoices.bollo``: the resolved virtual stamp duty (imposta di bollo)
  persisted with the totals. ``NOT NULL DEFAULT 0`` so every existing
  (non-forfettario) invoice keeps bollo 0 and ``total = taxable + vat``
  is unaffected; the forfettario rule (EUR 2.00 when taxable >= 77.47)
  only ever raises it for new forfettario documents.

``issuer_profiles``, ``client_profile`` and ``invoices`` are already
org-scoped + RLS (ENABLE + FORCE) with their existing policies and
flow_app grants; a plain column add inherits the table's existing
policy and grants, so no extra GRANT is needed (same as the
0029/0037/0041/0042 column-add migrations). Downgrade drops the three
columns, symmetrically.

Revision ID: 0043
Revises: 0042
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE issuer_profiles ADD COLUMN default_iban varchar(34) NULL",
    "ALTER TABLE client_profile ADD COLUMN payment_iban varchar(34) NULL",
    "ALTER TABLE invoices ADD COLUMN bollo numeric(14,2) NOT NULL DEFAULT 0",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE invoices DROP COLUMN IF EXISTS bollo",
    "ALTER TABLE client_profile DROP COLUMN IF EXISTS payment_iban",
    "ALTER TABLE issuer_profiles DROP COLUMN IF EXISTS default_iban",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
