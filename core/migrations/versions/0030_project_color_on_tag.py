"""Project colour lives on the tag, not the project profile.

0029 added ``project_profile.color``, but a project IS a tag (1:1) and
``tags.color`` already exists and already drives the chip rendered
everywhere a project tag appears. Two colour columns is a redundant
split with an obvious authority: the tag. Drop the profile column;
``create_project`` / ``update_project`` now set ``tags.color``.

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE project_profile DROP COLUMN IF EXISTS color")


def downgrade() -> None:
    op.execute("ALTER TABLE project_profile ADD COLUMN IF NOT EXISTS color varchar(16)")
