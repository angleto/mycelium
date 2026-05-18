"""W1b: login lockout counters on users (ported from
bitvision_phoenix; ADR-0024). Additive, DB-backed (shared state, not
per-process): repeated failed logins lock the account for a window.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE users ADD COLUMN failed_login_count integer NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN locked_until timestamptz",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE users DROP COLUMN IF EXISTS locked_until",
    "ALTER TABLE users DROP COLUMN IF EXISTS failed_login_count",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
