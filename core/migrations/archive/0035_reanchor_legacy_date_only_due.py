"""Re-anchor legacy date-only due dates to the owner's timezone.

Before the date-only fix, a "no time of day" deadline created via the MCP
was stored end-of-day in **UTC** (23:59:59Z). Now a date-only due is
anchored end-of-day in the OWNER's configured timezone, and the reminder
scanner detects "date-only" by that 23:59:59-in-the-owner's-zone sentinel.
So once an owner sets a non-UTC timezone, their old UTC end-of-day tasks
would no longer read as date-only (they'd land at e.g. 01:59 the next day)
and the calendar date would appear shifted.

This one-time data fix re-anchors each task whose due_date is *exactly*
23:59:59 UTC (the unambiguous legacy MCP sentinel) to 23:59:59 in the
owner's timezone, preserving the calendar date the user already sees.
Genuinely-timed dues (any other time-of-day) and dues already stored
end-of-day in a non-UTC zone (e.g. the SPA's old local convention) are
left untouched.

The re-anchor only does anything for owners with a non-UTC timezone, so
it also defaults the timezone to ``Europe/Rome`` for owners of such legacy
tasks whose timezone is still NULL (this deployment is single-user, per
the deploy README). All of this is a no-op on a database without legacy
23:59:59-UTC tasks (a fresh install), and idempotent (re-running re-anchors
nothing once the values already sit at end-of-day in the owner's zone).

Revision ID: 0035
Revises: 0034
Create Date: 2026-06-07
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The legacy MCP "no time of day" sentinel: end-of-day in UTC.
_LEGACY_TIME_UTC = "23:59:59"
_DEFAULT_TZ = "Europe/Rome"


def upgrade() -> None:
    conn = op.get_bind()
    # Owners of legacy 23:59:59-UTC date-only tasks who have no timezone yet
    # need one for the new owner-tz anchoring; default it (single-user prod).
    conn.execute(
        sa.text(
            "UPDATE users SET timezone = :tz "
            "WHERE timezone IS NULL AND id IN ("
            "  SELECT DISTINCT owner_id FROM tasks "
            "  WHERE due_date IS NOT NULL "
            "    AND (due_date AT TIME ZONE 'UTC')::time = :t)"
        ),
        {"tz": _DEFAULT_TZ, "t": _LEGACY_TIME_UTC},
    )
    # Re-anchor each legacy due to end-of-day in the owner's timezone,
    # keeping the same calendar date (DST handled by zoneinfo).
    rows = conn.execute(
        sa.text(
            "SELECT t.id, t.due_date, u.timezone FROM tasks t "
            "JOIN users u ON u.id = t.owner_id "
            "WHERE t.due_date IS NOT NULL "
            "  AND (t.due_date AT TIME ZONE 'UTC')::time = :t "
            "  AND u.timezone IS NOT NULL"
        ),
        {"t": _LEGACY_TIME_UTC},
    ).fetchall()
    moved = 0
    for tid, due, tzname in rows:
        try:
            tz = ZoneInfo(tzname)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=dt.UTC)
        # The UTC date is the day the user currently sees (their effective
        # zone was UTC when these were stored); pin it to end-of-day there.
        day = due.astimezone(dt.UTC).date()
        new = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=tz)
        if new == due:
            continue
        conn.execute(
            sa.text("UPDATE tasks SET due_date = :nd WHERE id = :id"),
            {"nd": new, "id": tid},
        )
        moved += 1
    print(f"0035: re-anchored {moved} legacy date-only due date(s) to owner timezone")


def downgrade() -> None:
    # Data fix; the prior UTC instants are not recoverable per-row. No-op.
    pass
