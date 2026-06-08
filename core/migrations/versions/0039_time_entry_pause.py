"""Pause/resume for the live timer: bank active time, keep the entry open.

A running timer stays a ``time_entries`` row with ``ended_at IS NULL``,
but it can now be *paused* without being finalized. Two columns make the
elapsed server-authoritative across pauses:

- ``accumulated_seconds``: active seconds banked from completed
  run-segments (everything before the current one). Frozen total while
  paused; summed with the live segment while running.
- ``resumed_at``: start of the CURRENT active segment. NOT NULL while
  running, NULL while paused (the segment has been banked) and NULL once
  stopped. ``started_at`` stays the original session start.

Live total = ``accumulated_seconds + (now - resumed_at)`` while running,
``accumulated_seconds`` while paused. On stop the live segment is banked
and the sum is written to ``duration_seconds`` (so reports, which read
``duration_seconds`` and exclude live rows, are unchanged).

Backfill keeps existing rows consistent: live rows were never paused, so
``resumed_at = started_at``; finalized rows get
``accumulated_seconds = duration_seconds`` (kept equal to the billed
total, though it is only read for live rows).

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "time_entries",
        sa.Column(
            "accumulated_seconds",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "time_entries",
        sa.Column("resumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # Live rows were never paused: the current segment started at
    # started_at, with nothing banked yet (accumulated_seconds already 0
    # from the column default). Finalized rows keep accumulated_seconds
    # equal to the billed total for a uniform "active seconds so far".
    op.execute("UPDATE time_entries SET resumed_at = started_at WHERE ended_at IS NULL")
    op.execute(
        "UPDATE time_entries SET accumulated_seconds = COALESCE(duration_seconds, 0) "
        "WHERE ended_at IS NOT NULL"
    )

    op.create_check_constraint(
        "ck_time_entries_accumulated",
        "time_entries",
        "accumulated_seconds >= 0",
    )
    # A finalized entry is not actively running: resumed_at must be NULL
    # once ended_at is set (running and paused both have ended_at NULL).
    op.create_check_constraint(
        "ck_time_entries_resumed",
        "time_entries",
        "ended_at IS NULL OR resumed_at IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_time_entries_resumed", "time_entries", type_="check")
    op.drop_constraint("ck_time_entries_accumulated", "time_entries", type_="check")
    op.drop_column("time_entries", "resumed_at")
    op.drop_column("time_entries", "accumulated_seconds")
