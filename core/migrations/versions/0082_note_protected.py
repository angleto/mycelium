"""notes.protected: Fase P of the search-informed graph (task 561c6aca,
plan si-procedi-a-sceiverla §7).

A single user-set boolean facet marking finished prose the distiller must
never compact: a ``protected`` note is excluded from ``is_inert`` and from
every distillation surface (distill / pattern / season sources and the
candidate listing). ADD COLUMN with a constant default is metadata-only on
PG11+ (no table rewrite, same trick as 0066); RLS/grants already cover the
``notes`` table.

Revision ID: 0082
Revises: 0081
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notes",
        sa.Column("protected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("notes", "protected")
