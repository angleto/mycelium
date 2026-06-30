"""Promote tasks.due_date from DATE to TIMESTAMPTZ.

Per-user request (task a3d1f5f4 item 3): the deadline must optionally
carry a time-of-day. Date-only entries backfill to end-of-day
(23:59:59 UTC) so a task "due tomorrow" still expires at the end of
the calendar day, not at midnight UTC.

Backwards-compatible read side: every caller that previously read a
``date`` now gets a ``datetime``; ``.date()`` recovers the calendar day
when needed. The matching service / Pydantic / SPA changes ship in the
same commit so the snapshot is internally consistent.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # USING-clause backfills the time-of-day: a date d becomes
    # "d 23:59:59" at UTC, matching the new "no time = end of day"
    # convention. Existing partial indexes / constraints reference
    # the column by name only, so the ALTER is transparent to them.
    op.execute(
        "ALTER TABLE tasks "
        "ALTER COLUMN due_date TYPE timestamptz "
        "USING ("
        "  (due_date::timestamp + interval '23 hours 59 minutes 59 seconds')"
        "  AT TIME ZONE 'UTC'"
        ")"
    )


def downgrade() -> None:
    # The reverse loses the time-of-day; date(due_date) at UTC is the
    # best we can do (an end-of-day entry rolls back to the same
    # calendar day, which is what callers expect).
    op.execute(
        "ALTER TABLE tasks "
        "ALTER COLUMN due_date TYPE date "
        "USING (due_date AT TIME ZONE 'UTC')::date"
    )
