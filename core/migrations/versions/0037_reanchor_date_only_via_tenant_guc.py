"""Re-anchor legacy date-only dues under RLS, via the tenant GUCs.

0035/0036 could not touch task rows on a deployment that FORCEs row-level
security: the migration role sees zero ``tasks`` (``p_tasks`` filters on
``current_setting('app.current_org')``, which a migration leaves unset).
This migration establishes the tenant context the app uses -- it reads the
global ``users`` table (no RLS), sets ``app.current_user`` to read that
user's org from ``memberships`` (``p_memberships_self_read``), then sets
``app.current_org`` so that user's ``tasks`` become visible -- and then
performs the same re-anchor as 0035/0036:

  for each task whose due_date is exactly 23:59:59 UTC (the legacy MCP
  "no time of day" sentinel), default the owner's timezone to Europe/Rome
  if it is NULL or resolves to UTC (single-user production), and re-store
  the due as 23:59:59 in the owner's timezone, preserving the calendar
  date (DST-correct via zoneinfo).

Only 23:59:59-UTC dues are touched (genuinely-timed and already-local dues
are left alone); idempotent; a no-op on a database with no such legacy
rows. The GUCs are set is_local (transaction-scoped) and cleared at the
end. Prints diagnostics to the migration log.

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-07
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_TIME_UTC = "23:59:59"
_DEFAULT_TZ = "Europe/Rome"
_WINTER = dt.datetime(2026, 1, 1)
_SUMMER = dt.datetime(2026, 7, 1)


def _is_utc_like(tzname: str | None) -> bool:
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

    def _set(guc: str, value: str) -> None:
        conn.execute(sa.text("SELECT set_config(:g, :v, true)"), {"g": guc, "v": value})

    users = conn.execute(sa.text("SELECT id, timezone FROM users")).fetchall()
    seen = 0
    tz_set = 0
    moved = 0
    defaulted: set[str] = set()
    for uid, tzname in users:
        _set("app.current_user", str(uid))
        # Explicit user filter (not just the self_read RLS policy): on a
        # database where the migration role is NOT RLS-filtered this avoids
        # scanning every org; on a FORCE-RLS database it is redundant with
        # ``p_memberships_self_read`` (app.current_user) and equally correct.
        orgs = conn.execute(
            sa.text("SELECT DISTINCT org_id FROM memberships WHERE user_id = :uid"),
            {"uid": uid},
        ).fetchall()
        for (org_id,) in orgs:
            _set("app.current_org", str(org_id))
            rows = conn.execute(
                sa.text(
                    "SELECT id, due_date FROM tasks WHERE owner_id = :uid "
                    "AND due_date IS NOT NULL "
                    "AND (due_date AT TIME ZONE 'UTC')::time = :t"
                ),
                {"uid": uid, "t": _LEGACY_TIME_UTC},
            ).fetchall()
            if not rows:
                continue
            seen += len(rows)
            eff = tzname
            if _is_utc_like(tzname):
                conn.execute(
                    sa.text("UPDATE users SET timezone = :tz WHERE id = :id"),
                    {"tz": _DEFAULT_TZ, "id": uid},
                )
                eff = _DEFAULT_TZ
                tzname = _DEFAULT_TZ  # don't re-default for this user's other orgs
                if str(uid) not in defaulted:
                    defaulted.add(str(uid))
                    tz_set += 1
            try:
                tz = ZoneInfo(eff)
            except (ZoneInfoNotFoundError, ValueError):
                continue
            for tid, due in rows:
                if due.tzinfo is None:
                    due = due.replace(tzinfo=dt.UTC)
                new = dt.datetime.combine(
                    due.astimezone(dt.UTC).date(), dt.time(23, 59, 59), tzinfo=tz
                )
                if new == due:
                    continue
                conn.execute(
                    sa.text("UPDATE tasks SET due_date = :nd WHERE id = :id"),
                    {"nd": new, "id": tid},
                )
                moved += 1
    _set("app.current_org", "")
    _set("app.current_user", "")
    print(
        f"0037: users={len(users)}, legacy dues seen={seen}, "
        f"timezones defaulted to {_DEFAULT_TZ}={tz_set}, re-anchored={moved}"
    )


def downgrade() -> None:
    # Data fix; prior UTC instants are not recoverable per-row. No-op.
    pass
