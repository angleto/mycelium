"""Add users.day_start_minute: when a date-only task's reminders fire.

A date-only deadline (``tasks.due_date`` stored at end-of-day in the
owner's timezone) used to fire its "at due" reminder at 23:59:59 -- the
end of the day -- which reads as a day late. ``scan_reminders`` now
anchors date-only reminders to the START of the due day plus this
per-user offset (minutes after local midnight, in ``users.timezone``).
0 = local midnight (the default, "start of day"); 360 = 06:00. The
end-of-day expiry/overdue semantics are unchanged.

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "day_start_minute",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "day_start_minute")
