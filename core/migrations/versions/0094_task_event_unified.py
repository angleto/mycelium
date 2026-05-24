"""Unify appointments into tasks: ``start_at`` + ``duration_minutes`` +
``recurrence`` columns + GiST EXCLUDE constraint enforcing no-ubiquity
per ``assignee_id`` (ADR-0008 addendum, see docs/adr/0008-no-ubiquity-events.md).

Design:
- ``start_at`` (timestamptz) and ``duration_minutes`` (int) are paired:
  either both set (task IS a calendar appointment) or both NULL (plain
  task / reminder). ``due_date`` (date) stays as the legacy deadline
  field used by reminders and unscheduled work; appointments use
  ``start_at`` for their actual moment in time.
- ``recurrence`` (jsonb) carries the recurrence spec for both reminders
  and appointments. The recurrence engine consumes it; this migration
  only provisions the column.
- The EXCLUDE constraint replaces the dedicated ``events`` table's
  overlap logic (events table is dropped in a follow-up migration once
  the routers are migrated).
- ``btree_gist`` is required to mix ``uuid =`` with ``tstzrange &&`` in
  the same exclusion. ``pg_trgm`` is already enabled in baseline so
  precedent is set.

Revision: 0094
Down revision: 0093
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0094"
down_revision: str | None = "0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS btree_gist",
    # ``timestamptz + interval`` is STABLE (an interval can carry
    # month/year units whose actual length depends on TimeZone), so
    # Postgres rejects it inside an index expression. Wrap the
    # minute-only addition in a SQL function marked IMMUTABLE: only
    # minutes are ever added (no calendar units), so the result is
    # deterministic regardless of TZ. Standard pattern for adding
    # numeric intervals to timestamps inside index / EXCLUDE
    # expressions.
    """
    CREATE OR REPLACE FUNCTION tasks_event_end(t timestamptz, m integer)
    RETURNS timestamptz
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    AS $fn$
      SELECT t + make_interval(mins => m)
    $fn$
    """,
    """
    ALTER TABLE tasks
      ADD COLUMN start_at timestamptz,
      ADD COLUMN duration_minutes integer,
      ADD COLUMN recurrence jsonb
    """,
    # start_at and duration_minutes are paired: an appointment has both,
    # a plain task has neither. Mixed states are rejected.
    """
    ALTER TABLE tasks
      ADD CONSTRAINT ck_tasks_event_pairing
      CHECK ((start_at IS NULL) = (duration_minutes IS NULL))
    """,
    """
    ALTER TABLE tasks
      ADD CONSTRAINT ck_tasks_duration_positive
      CHECK (duration_minutes IS NULL OR duration_minutes > 0)
    """,
    # No-ubiquity per assignee: two appointments of the same identity
    # cannot overlap in time. Predicate skips plain tasks, archived /
    # deleted rows, and rows with NULL assignee (orphan event has no
    # one to conflict with). The interval is computed from the pair
    # ``(start_at, duration_minutes)`` via ``make_interval`` (IMMUTABLE,
    # required by index expressions; the text-based
    # ``(duration_minutes || ' minutes')::interval`` form is not).
    """
    ALTER TABLE tasks
      ADD CONSTRAINT no_overlap_event_tasks_per_assignee
      EXCLUDE USING gist (
        assignee_id WITH =,
        tstzrange(
          start_at,
          tasks_event_end(start_at, duration_minutes)
        ) WITH &&
      )
      WHERE (
        duration_minutes IS NOT NULL
        AND start_at IS NOT NULL
        AND assignee_id IS NOT NULL
        AND is_archived = false
        AND deleted_at IS NULL
      )
    """,
    # Partial index for the appointment-list query path used by the
    # calendar view (``WHERE duration_minutes IS NOT NULL``).
    """
    CREATE INDEX ix_tasks_event_start_at
      ON tasks (start_at)
      WHERE duration_minutes IS NOT NULL
    """,
)


DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_tasks_event_start_at",
    "ALTER TABLE tasks DROP CONSTRAINT IF EXISTS no_overlap_event_tasks_per_assignee",
    "ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_duration_positive",
    "ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_event_pairing",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS recurrence",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS duration_minutes",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS start_at",
    "DROP FUNCTION IF EXISTS tasks_event_end(timestamptz, integer)",
    # btree_gist is left in place — other migrations may rely on it.
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
