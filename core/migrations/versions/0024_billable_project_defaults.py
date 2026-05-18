"""Billable as a project-profile default + per-task override.

``project_profile.default_billable`` is an automatic project property
(alongside tariffa/valuta/budget). ``tasks.billable`` is a nullable
override: NULL = inherit the project default (or true with no
project); true/false = explicit. Effective billability is resolved
at timer/manual-entry time (same path as the rate snapshot).

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE project_profile "
    "ADD COLUMN IF NOT EXISTS default_billable boolean NOT NULL DEFAULT true",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS billable boolean",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE tasks DROP COLUMN IF EXISTS billable",
    "ALTER TABLE project_profile DROP COLUMN IF EXISTS default_billable",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
