"""Re-anchor legacy date-only dues for UTC-effective owners (follow-up to 0035).

0035 re-anchored legacy 23:59:59-UTC date-only dues to the owner's
timezone, but only DEFAULTED the timezone (to Europe/Rome) for owners
whose ``timezone`` was NULL. An owner whose timezone is set to an explicit
UTC-equivalent value ("UTC" / "Etc/UTC") was therefore left as-is, and the
re-anchor was a no-op for them (23:59:59 UTC -> 23:59:59 UTC).

This deployment's owner has an explicit UTC timezone and has asked to move
to Europe/Rome with their existing date-only tasks fixed. So: for owners of
legacy 23:59:59-UTC date-only tasks whose timezone is NULL *or resolves to
UTC*, set it to Europe/Rome (single-user production), then re-anchor those
dues to 23:59:59 in the owner's timezone, preserving the calendar date
(DST-correct via zoneinfo).

Self-limiting: only owners of legacy 23:59:59-UTC tasks are touched, so it
is a no-op on a fresh install (no such tasks) and idempotent (once a due
sits at end-of-day in the owner's non-UTC zone it is left alone). Prints
diagnostics (task visibility, counts) to the migration log.

Revision ID: 0036
Revises: 0035
Create Date: 2026-06-07
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_TIME_UTC = "23:59:59"
_DEFAULT_TZ = "Europe/Rome"
_WINTER = dt.datetime(2026, 1, 1)
_SUMMER = dt.datetime(2026, 7, 1)


def _is_utc_like(tzname: str | None) -> bool:
    """True if the timezone is unset or has a zero UTC offset year-round
    (UTC / Etc/UTC / GMT ...). Such an owner needs a real zone for the
    new owner-tz date-only anchoring."""
    if not tzname:
        return True
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        return True
    zero = dt.timedelta(0)
    return tz.utcoffset(_WINTER) == zero and tz.utcoffset(_SUMMER) == zero


def upgrade() -> None:
    conn = op.get_bind()
    # Diagnostic: confirm the migration role can see task rows (RLS sanity).
    total = conn.execute(sa.text("SELECT count(*) FROM tasks")).scalar_one()
    legacy = conn.execute(
        sa.text(
            "SELECT count(*) FROM tasks WHERE due_date IS NOT NULL "
            "AND (due_date AT TIME ZONE 'UTC')::time = :t"
        ),
        {"t": _LEGACY_TIME_UTC},
    ).scalar_one()
    print(f"0036: tasks visible={total}, legacy 23:59:59-UTC date-only={legacy}")

    # Default the timezone to Europe/Rome for owners of legacy date-only
    # tasks whose timezone is NULL or resolves to UTC.
    owner_rows = conn.execute(
        sa.text(
            "SELECT DISTINCT u.id, u.timezone FROM users u "
            "JOIN tasks t ON t.owner_id = u.id "
            "WHERE t.due_date IS NOT NULL "
            "AND (t.due_date AT TIME ZONE 'UTC')::time = :t"
        ),
        {"t": _LEGACY_TIME_UTC},
    ).fetchall()
    tz_set = 0
    for uid, tzname in owner_rows:
        if _is_utc_like(tzname):
            conn.execute(
                sa.text("UPDATE users SET timezone = :tz WHERE id = :id"),
                {"tz": _DEFAULT_TZ, "id": uid},
            )
            tz_set += 1

    # Re-anchor each legacy due to end-of-day in the owner's timezone,
    # preserving the calendar date.
    rows = conn.execute(
        sa.text(
            "SELECT t.id, t.due_date, u.timezone FROM tasks t "
            "JOIN users u ON u.id = t.owner_id "
            "WHERE t.due_date IS NOT NULL "
            "AND (t.due_date AT TIME ZONE 'UTC')::time = :t "
            "AND u.timezone IS NOT NULL"
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
        day = due.astimezone(dt.UTC).date()
        new = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=tz)
        if new == due:
            continue
        conn.execute(
            sa.text("UPDATE tasks SET due_date = :nd WHERE id = :id"),
            {"nd": new, "id": tid},
        )
        moved += 1
    print(f"0036: timezones defaulted to {_DEFAULT_TZ}={tz_set}, dues re-anchored={moved}")


def downgrade() -> None:
    # Data fix; the prior UTC instants are not recoverable per-row. No-op.
    pass
