"""Add ``humus_kind`` + ``humus_flag`` columns on ``notes`` (task 4a718dc4).

The two columns power the "decomposizione fungina" pipeline:

- ``humus_kind`` (text, nullable) tags a note as the *output* of a
  decomposition step. ``distillation`` is one note distilled from one
  source on archive; ``pattern`` aggregates several notes inside a
  Leiden cluster; ``season`` is a quarterly synthesis. NULL means the
  note is regular user content.
- ``humus_flag`` (bool, default false) marks a note as eligible to be
  surfaced as humus in the LLM walk (ADR-0034). The distillation
  pipeline sets it; users can toggle it manually for legacy notes.

Both are additive; no data backfill needed.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notes",
        sa.Column("humus_kind", sa.String(32), nullable=True),
    )
    op.add_column(
        "notes",
        sa.Column(
            "humus_flag",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_notes_humus_flag",
        "notes",
        ["org_id", "humus_flag"],
        postgresql_where=sa.text("humus_flag = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_notes_humus_flag", table_name="notes")
    op.drop_column("notes", "humus_flag")
    op.drop_column("notes", "humus_kind")
