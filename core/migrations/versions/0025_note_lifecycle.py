"""Notes: archive + soft-delete (mirrors tasks).

Notes become archivable and deletable like tasks: ``is_archived`` and
``deleted_at`` hide them from the default list and surface in the
trash & archive view; both are reversible.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS is_archived boolean NOT NULL DEFAULT false",
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS deleted_at timestamptz",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE notes DROP COLUMN IF EXISTS deleted_at",
    "ALTER TABLE notes DROP COLUMN IF EXISTS is_archived",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
