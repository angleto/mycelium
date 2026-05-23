"""task.owner_id (accountability) + task.assignee_id (intent via
Identity) — docs/adr/0028 Stage 2 of 3.

Adds the two new columns and backfills from existing data:

- ``owner_id``: every existing task inherits ``created_by`` as owner.
  For tasks where ``created_by`` is NULL (legacy / system-created
  rows), we fall back to the workspace owner. ``ON DELETE RESTRICT``
  refuses to delete a user that still owns tasks.
- ``assignee_id``: resolved from the existing ``assignee_handle``
  via the identities table populated in 0084. Tasks without an
  assignee_handle keep ``assignee_id`` NULL.

The mirror columns (``executor_kind``, ``executor_user_id``,
``assignee_handle``) STAY in this migration. They are dropped in
0087 after the refactor commit updates every reader.

Revision: 0086
Down revision: 0085
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    # Add the columns nullable first so the backfill can run; then
    # add NOT NULL to owner_id once it is populated.
    "ALTER TABLE tasks ADD COLUMN owner_id uuid REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE tasks ADD COLUMN assignee_id uuid REFERENCES identities(id) ON DELETE SET NULL",
    # Backfill owner_id. On managed Postgres the migration role
    # (table owner ``flow``) is not BYPASSRLS and, in this deployment,
    # even though Postgres normally exempts the owner from
    # ENABLE-RLS policies, plain ``UPDATE tasks SET ... WHERE ...``
    # touches zero rows while the DDL ``SET NOT NULL`` still scans
    # every physical row. To make the backfill see everything,
    # disable RLS on ``tasks`` for the duration of the DML (the
    # table owner is allowed to ALTER ENABLE/DISABLE without
    # BYPASSRLS), then re-enable. Fallback order:
    #   created_by -> earliest 'owner' membership -> earliest any
    #   membership.
    "ALTER TABLE tasks DISABLE ROW LEVEL SECURITY",
    """
    UPDATE tasks t
    SET owner_id = COALESCE(
      t.created_by,
      (
        SELECT m.user_id FROM memberships m
        WHERE m.org_id = t.org_id AND m.role = 'owner'
        ORDER BY m.created_at, m.user_id LIMIT 1
      ),
      (
        SELECT m.user_id FROM memberships m
        WHERE m.org_id = t.org_id
        ORDER BY m.created_at, m.user_id LIMIT 1
      )
    )
    WHERE t.owner_id IS NULL
    """,
    """
    DO $check_owner$
    BEGIN
      IF EXISTS (SELECT 1 FROM tasks WHERE owner_id IS NULL) THEN
        RAISE EXCEPTION
          'tasks with NULL owner_id remain after backfill'
          USING HINT = 'check workspace memberships';
      END IF;
    END
    $check_owner$
    """,
    "ALTER TABLE tasks ALTER COLUMN owner_id SET NOT NULL",
    # Backfill assignee_id from assignee_handle. RLS still disabled
    # from above; we re-enable at the end.
    """
    UPDATE tasks t
    SET assignee_id = i.id
    FROM identities i
    WHERE t.assignee_handle IS NOT NULL
      AND t.assignee_handle <> ''
      AND i.org_id = t.org_id
      AND i.handle = t.assignee_handle
    """,
    # Re-enable RLS now that both backfills are done. ``tasks``
    # is FORCE RLS in baseline (migration 0034), so we must
    # restore BOTH flags or the table owner would silently bypass
    # tenant isolation. Policy rows themselves are untouched.
    "ALTER TABLE tasks ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE tasks FORCE ROW LEVEL SECURITY",
    "CREATE INDEX ix_tasks_owner_id ON tasks (owner_id)",
    "CREATE INDEX ix_tasks_assignee_id ON tasks (assignee_id)",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_tasks_assignee_id",
    "DROP INDEX IF EXISTS ix_tasks_owner_id",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS assignee_id",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS owner_id",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
