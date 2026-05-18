"""Add 'yearly' to the recurrence_freq enum (additive).

``ALTER TYPE ... ADD VALUE`` runs outside the migration transaction
(autocommit block): Postgres forbids using a new enum label in the
same transaction that added it. Idempotent via IF NOT EXISTS.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE recurrence_freq ADD VALUE IF NOT EXISTS 'yearly'"
        )


def downgrade() -> None:
    # Postgres cannot drop a single enum label; leaving 'yearly' in the
    # type is harmless (no rows reference it after a downgrade of the
    # feature). Intentional no-op.
    pass
