"""task_participants: extra identities pinned to an appointment-task.

A task with ``start_at`` + ``duration_minutes`` (migration 0094) is a
calendar appointment. The ``assignee_id`` already enforces no-ubiquity
for that one identity via the EXCLUDE on ``tasks``. This migration adds
``task_participants`` so the same overlap protection extends to N
*additional* identities that are pinned to the appointment: if user A
and user B both participate in event E (9:00-10:00), neither A nor B
may hold another appointment overlapping that window.

Design notes:

- The participant row carries ``start_at`` + ``duration_minutes``
  denormalised from the task. A GiST EXCLUDE on the participant table
  is the cleanest way to enforce per-identity non-overlap; index
  expressions cannot reach into a joined table, so the window must
  live on the row being constrained.
- A trigger ``sync_task_participants_window`` keeps the denormalised
  columns in sync when the parent task's window moves, and deletes
  all participants if the task loses its appointment status
  (``duration_minutes`` set back to NULL).
- The assignee is **not** auto-inserted as a participant: the
  ``tasks.no_overlap_event_tasks_per_assignee`` EXCLUDE already covers
  them. ``task_participants`` is strictly for *additional* identities.
  This avoids duplicated enforcement and keeps the assignee column
  authoritative for the primary owner of the appointment.

Revision: 0095
Down revision: 0094
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"


UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE task_participants (
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      identity_id uuid NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      start_at timestamptz NOT NULL,
      duration_minutes integer NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_task_participants PRIMARY KEY (task_id, identity_id),
      CONSTRAINT ck_task_participants_duration_pos
        CHECK (duration_minutes > 0)
    )
    """,
    "CREATE INDEX ix_task_participants_org_id ON task_participants (org_id)",
    "CREATE INDEX ix_task_participants_identity_id ON task_participants (identity_id)",
    "CREATE INDEX ix_task_participants_task_id ON task_participants (task_id)",
    # No-ubiquity for additional participants. Mirrors the tasks-level
    # EXCLUDE for the assignee: same range expression via the shared
    # IMMUTABLE helper ``tasks_event_end`` (migration 0094).
    """
    ALTER TABLE task_participants
      ADD CONSTRAINT no_overlap_task_participants
      EXCLUDE USING gist (
        identity_id WITH =,
        tstzrange(start_at, tasks_event_end(start_at, duration_minutes)) WITH &&
      )
    """,
    # Sync trigger: propagate window changes on the parent task to
    # every participant row, so the EXCLUDE re-evaluates with the new
    # window. If the task drops its appointment status (duration_minutes
    # NULL), all participants are removed -- the row is no longer a
    # calendar block.
    """
    CREATE OR REPLACE FUNCTION sync_task_participants_window()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    BEGIN
      IF NEW.duration_minutes IS NULL OR NEW.start_at IS NULL THEN
        DELETE FROM task_participants WHERE task_id = NEW.id;
      ELSE
        UPDATE task_participants
           SET start_at = NEW.start_at,
               duration_minutes = NEW.duration_minutes
         WHERE task_id = NEW.id
           AND (start_at IS DISTINCT FROM NEW.start_at
                OR duration_minutes IS DISTINCT FROM NEW.duration_minutes);
      END IF;
      RETURN NEW;
    END
    $fn$
    """,
    "REVOKE ALL ON FUNCTION sync_task_participants_window() FROM PUBLIC",
    """
    CREATE TRIGGER trg_sync_task_participants_window
      AFTER UPDATE OF start_at, duration_minutes ON tasks
      FOR EACH ROW
      EXECUTE FUNCTION sync_task_participants_window()
    """,
    "ALTER TABLE task_participants ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE task_participants FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_task_participants ON task_participants "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON task_participants TO flow_app",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_sync_task_participants_window ON tasks",
    "DROP FUNCTION IF EXISTS sync_task_participants_window()",
    "DROP TABLE IF EXISTS task_participants CASCADE",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
