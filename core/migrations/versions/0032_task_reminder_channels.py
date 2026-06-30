"""Per-reminder channel selection: task_reminders.channels.

A reminder may pin which notification channels it fires on (a JSON list of
NotificationChannelKind values, e.g. ["email", "telegram"]). NULL keeps the
existing behaviour -- the reminder uses each recipient's default (all their
enabled channel prefs). Existing rows stay NULL, so the change is
backward-compatible.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_reminders",
        sa.Column("channels", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_reminders", "channels")
