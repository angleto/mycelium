"""Add notes.auto_cluster + notes.auto_classified_at: autonomous
classify-on-ingest marker (WS-D2 / ADR-0032 P4).

The garden sweep stamps each not-yet-seen note with the structural Leiden
community the offline graph snapshot already computed for it
(``auto_cluster``), plus the time it was first auto-classified
(``auto_classified_at``; NULL = never seen by the autonomous pass).
Read-only: the opinionated tag/link/maturity suggestions stay
human-applied via the live classify panel.

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("auto_cluster", sa.Integer(), nullable=True))
    op.add_column(
        "notes",
        sa.Column("auto_classified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notes", "auto_classified_at")
    op.drop_column("notes", "auto_cluster")
