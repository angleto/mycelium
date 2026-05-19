"""Per-workspace settings (additive): a JSONB bag on organizations.

First use: configurable task-estimate presets (the dropdown values on
the task form). Workspace-scoped, persisted and shared (not client
local). Generic JSONB so further small workspace prefs do not each
need a migration.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS settings jsonb NOT NULL DEFAULT '{}'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS settings")
