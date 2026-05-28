"""Optional title column on note_part.

A user-facing label for a part, distinct from the part's body. Useful
when a note is composed of named sections (e.g. "Intro", "Acceptance
criteria"); when omitted (the default) the SPA falls back to the
first non-empty line of the body, preserving the current UX.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "note_part",
        sa.Column("title", sa.String(length=300), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("note_part", "title")
