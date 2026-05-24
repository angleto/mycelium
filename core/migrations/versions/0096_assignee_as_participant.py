"""Unify no-ubiquity onto ``task_participants``: drop the
``no_overlap_event_tasks_per_assignee`` EXCLUDE from ``tasks`` and
move all overlap enforcement to ``task_participants``. A trigger
mirrors the assignee into the participants table whenever the task
carries an appointment window, so the single EXCLUDE
(``no_overlap_task_participants``) becomes the authoritative
no-ubiquity rule for both the primary assignee and every additional
participant.

Why this consolidation:

The 0094 EXCLUDE only saw assignee-vs-assignee collisions; the 0095
EXCLUDE only saw participant-vs-participant. The mixed case
"identity X is the assignee of event A and a participant of event B"
slipped through both. Auto-inserting the assignee into
``task_participants`` collapses both axes into a single per-identity
ledger, so any new appointment (or move, or participant add) re-checks
against the union.

Revision: 0096
Down revision: 0095
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    # Drop the tasks-level EXCLUDE: redundant with the consolidated
    # participant-level one once the trigger mirrors the assignee.
    "ALTER TABLE tasks DROP CONSTRAINT IF EXISTS no_overlap_event_tasks_per_assignee",
    # The trigger inserts / updates / deletes the assignee's row in
    # task_participants in lockstep with the task's
    # ``(assignee_id, start_at, duration_minutes)`` triple. It runs
    # AFTER the row write so the parent row exists for FK lookup.
    """
    CREATE OR REPLACE FUNCTION sync_task_assignee_participant()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    DECLARE
      v_old_assignee uuid;
      v_new_is_appt boolean;
    BEGIN
      v_old_assignee := CASE WHEN TG_OP = 'UPDATE' THEN OLD.assignee_id ELSE NULL END;
      v_new_is_appt := (
        NEW.assignee_id IS NOT NULL
        AND NEW.start_at IS NOT NULL
        AND NEW.duration_minutes IS NOT NULL
        AND NEW.is_archived = false
        AND NEW.deleted_at IS NULL
      );
      -- Drop the stale assignee mirror when the assignee changes,
      -- the appointment status is lost, or the row is archived/soft-
      -- deleted. ``ON CONFLICT DO NOTHING`` is not enough -- the row
      -- may need to be removed entirely.
      IF v_old_assignee IS NOT NULL
         AND (NOT v_new_is_appt OR v_old_assignee <> NEW.assignee_id) THEN
        DELETE FROM task_participants
          WHERE task_id = NEW.id AND identity_id = v_old_assignee;
      END IF;
      IF v_new_is_appt THEN
        INSERT INTO task_participants
          (task_id, identity_id, org_id, start_at, duration_minutes)
        VALUES
          (NEW.id, NEW.assignee_id, NEW.org_id,
           NEW.start_at, NEW.duration_minutes)
        ON CONFLICT (task_id, identity_id) DO UPDATE
          SET start_at = EXCLUDED.start_at,
              duration_minutes = EXCLUDED.duration_minutes;
      END IF;
      RETURN NEW;
    END
    $fn$
    """,
    "REVOKE ALL ON FUNCTION sync_task_assignee_participant() FROM PUBLIC",
    # AFTER INSERT and AFTER UPDATE OF the four relevant columns are
    # enough: changes to title / priority / tags / etc. do not move the
    # mirror.
    """
    CREATE TRIGGER trg_sync_task_assignee_participant_ins
      AFTER INSERT ON tasks
      FOR EACH ROW
      EXECUTE FUNCTION sync_task_assignee_participant()
    """,
    """
    CREATE TRIGGER trg_sync_task_assignee_participant_upd
      AFTER UPDATE OF assignee_id, start_at, duration_minutes,
                       is_archived, deleted_at ON tasks
      FOR EACH ROW
      EXECUTE FUNCTION sync_task_assignee_participant()
    """,
    # Backfill: every live appointment-task with an assignee gets a
    # participant row. RLS is disabled on tasks for the migration role
    # (matches the 0086 backfill pattern); INSERT into task_participants
    # via SECURITY DEFINER would be ideal but the migration role can
    # write directly. We disable + restore RLS on task_participants for
    # the bulk insert.
    "ALTER TABLE task_participants DISABLE ROW LEVEL SECURITY",
    "ALTER TABLE tasks DISABLE ROW LEVEL SECURITY",
    """
    INSERT INTO task_participants
      (task_id, identity_id, org_id, start_at, duration_minutes)
    SELECT
      t.id, t.assignee_id, t.org_id, t.start_at, t.duration_minutes
    FROM tasks t
    WHERE t.assignee_id IS NOT NULL
      AND t.start_at IS NOT NULL
      AND t.duration_minutes IS NOT NULL
      AND t.is_archived = false
      AND t.deleted_at IS NULL
    ON CONFLICT (task_id, identity_id) DO NOTHING
    """,
    "ALTER TABLE tasks ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE tasks FORCE ROW LEVEL SECURITY",
    "ALTER TABLE task_participants ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE task_participants FORCE ROW LEVEL SECURITY",
)


DOWNGRADE: tuple[str, ...] = (
    # Re-instate the tasks-level EXCLUDE first so the assignee axis
    # is still protected after the trigger is removed.
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
    "DROP TRIGGER IF EXISTS trg_sync_task_assignee_participant_upd ON tasks",
    "DROP TRIGGER IF EXISTS trg_sync_task_assignee_participant_ins ON tasks",
    "DROP FUNCTION IF EXISTS sync_task_assignee_participant()",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
