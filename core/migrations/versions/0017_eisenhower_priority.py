"""Eisenhower priority inputs on tasks (additive).

``importance`` and ``urgency`` (1..5) are persisted so the matrix
round-trips; the service derives the existing 1..4 ``priority`` from
their product (1 = highest, ADR-0004). Nullable: the legacy priority
path and pre-migration tasks leave them unset. flow_app grants are
table-level (new columns covered).

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE tasks ADD COLUMN importance smallint",
    "ALTER TABLE tasks ADD COLUMN urgency smallint",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE tasks DROP COLUMN IF EXISTS urgency",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS importance",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
