"""Add users.timezone (IANA) for local-time reminder rendering.

``scan_reminders`` renders a reminder's human label in the recipient's
timezone and detects the date-only ("no time set") sentinel in that
timezone (the SPA stores an unspecified due time as end-of-day LOCAL,
which is 23:59:59 only in the user's own timezone). NULL = UTC, the
pre-0019 behaviour, so existing rows keep working unchanged.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("timezone", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "timezone")
