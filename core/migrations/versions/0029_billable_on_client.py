"""Billable moves to the client; project gains colour + description.

Billing is a client relationship, not a per-project trait, so the
automatic billable default lives on ``client_profile`` now (was
``project_profile.default_billable``). Projects keep budget/rate and
gain an optional UI ``color`` and a free ``description``; clients gain
a free ``description``. Both descriptions are useful AI context
(docs/adr/0005).

Existing rows: every client defaults to billable=true (the prior
global default); the per-project flag is dropped (the model changed,
the nuance is not carried back).

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE client_profile ADD COLUMN IF NOT EXISTS description text",
    "ALTER TABLE client_profile ADD COLUMN IF NOT EXISTS default_billable "
    "boolean NOT NULL DEFAULT true",
    "ALTER TABLE project_profile ADD COLUMN IF NOT EXISTS color varchar(16)",
    "ALTER TABLE project_profile ADD COLUMN IF NOT EXISTS description text",
    "ALTER TABLE project_profile DROP COLUMN IF EXISTS default_billable",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE project_profile ADD COLUMN IF NOT EXISTS default_billable "
    "boolean NOT NULL DEFAULT true",
    "ALTER TABLE project_profile DROP COLUMN IF EXISTS description",
    "ALTER TABLE project_profile DROP COLUMN IF EXISTS color",
    "ALTER TABLE client_profile DROP COLUMN IF EXISTS default_billable",
    "ALTER TABLE client_profile DROP COLUMN IF EXISTS description",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
