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
    # Backfill owner_id. RLS-aware: managed Postgres deployments run
    # alembic as a role without BYPASSRLS, so a plain ``UPDATE tasks
    # SET ... WHERE ...`` only touches rows the role currently sees
    # (typically zero, fail-closed). We loop per org, setting
    # ``app.current_org`` so the RLS policy admits the org's rows
    # within each iteration; the DDL ``ALTER ... SET NOT NULL`` later
    # still runs over every physical row, so the rows we miss here
    # would crash the migration. Fallback order:
    #   created_by -> earliest 'owner' membership -> earliest any
    #   membership. If none, raise with a clear hint.
    """
    DO $bf_owner$
    DECLARE
      v_org_id uuid;
      v_left  bigint;
    BEGIN
      FOR v_org_id IN SELECT id FROM organizations LOOP
        PERFORM set_config('app.current_org', v_org_id::text, true);
        UPDATE tasks t
        SET owner_id = COALESCE(
          t.created_by,
          (
            SELECT m.user_id FROM memberships m
            WHERE m.org_id = v_org_id AND m.role = 'owner'
            ORDER BY m.created_at, m.user_id LIMIT 1
          ),
          (
            SELECT m.user_id FROM memberships m
            WHERE m.org_id = v_org_id
            ORDER BY m.created_at, m.user_id LIMIT 1
          )
        )
        WHERE t.org_id = v_org_id AND t.owner_id IS NULL;
      END LOOP;
      PERFORM set_config('app.current_org', '', true);
      -- Recheck: the SELECT count below runs without a tenant GUC,
      -- but RLS on tasks for the migration role still hides rows.
      -- We instead consult pg_stat with a privileged probe via a
      -- SECURITY DEFINER function would be ideal; absent that, we
      -- iterate orgs again and count visible NULLs which equals
      -- the real count under the same RLS path used for the UPDATE.
      v_left := 0;
      FOR v_org_id IN SELECT id FROM organizations LOOP
        PERFORM set_config('app.current_org', v_org_id::text, true);
        v_left := v_left + (
          SELECT COUNT(*) FROM tasks WHERE owner_id IS NULL
        );
      END LOOP;
      PERFORM set_config('app.current_org', '', true);
      IF v_left > 0 THEN
        RAISE EXCEPTION 'tasks with NULL owner_id remain after backfill: %', v_left
          USING HINT = 'check workspace memberships';
      END IF;
    END
    $bf_owner$
    """,
    "ALTER TABLE tasks ALTER COLUMN owner_id SET NOT NULL",
    # Backfill assignee_id from assignee_handle. Same RLS-aware
    # per-org loop pattern as the owner_id backfill above.
    """
    DO $bf_assignee$
    DECLARE
      v_org_id uuid;
    BEGIN
      FOR v_org_id IN SELECT id FROM organizations LOOP
        PERFORM set_config('app.current_org', v_org_id::text, true);
        UPDATE tasks t
        SET assignee_id = i.id
        FROM identities i
        WHERE t.org_id = v_org_id
          AND t.assignee_handle IS NOT NULL
          AND t.assignee_handle <> ''
          AND i.org_id = t.org_id
          AND i.handle = t.assignee_handle;
      END LOOP;
      PERFORM set_config('app.current_org', '', true);
    END
    $bf_assignee$
    """,
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
