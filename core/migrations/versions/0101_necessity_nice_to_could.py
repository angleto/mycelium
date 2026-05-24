"""Rename Necessity enum value ``nice`` to ``could`` to align with the
standard MoSCoW vocabulary (Must / Should / Could). Pure label rename
on the PG enum: existing rows keep their ordinal, the new label is
visible to every consumer immediately. MCP callers can finally pass
``could`` (the natural MoSCoW word) without tripping a ValueError on
``Necessity(value)``.

Revision ID: 0101
Revises: 0100
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0101"
down_revision: str | None = "0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE necessity RENAME VALUE 'nice' TO 'could'")


def downgrade() -> None:
    op.execute("ALTER TYPE necessity RENAME VALUE 'could' TO 'nice'")
