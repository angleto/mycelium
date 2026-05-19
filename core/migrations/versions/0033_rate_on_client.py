"""Hourly rate moves to the client (like billable).

The rate (tariffa/valuta) is a client relationship, not a per-project
trait, so it lives on ``client_profile`` now. ``budget`` stays on the
project. Existing per-project rates are dropped (the model changed; the
nuance is not carried back) — clients start with no rate (EUR).

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE client_profile ADD COLUMN IF NOT EXISTS tariffa numeric(12, 2)",
    "ALTER TABLE client_profile ADD COLUMN IF NOT EXISTS valuta varchar(3) NOT NULL DEFAULT 'EUR'",
    "ALTER TABLE project_profile DROP COLUMN IF EXISTS tariffa",
    "ALTER TABLE project_profile DROP COLUMN IF EXISTS valuta",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE project_profile ADD COLUMN IF NOT EXISTS tariffa numeric(12, 2)",
    "ALTER TABLE project_profile ADD COLUMN IF NOT EXISTS valuta varchar(3) NOT NULL DEFAULT 'EUR'",
    "ALTER TABLE client_profile DROP COLUMN IF EXISTS valuta",
    "ALTER TABLE client_profile DROP COLUMN IF EXISTS tariffa",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
