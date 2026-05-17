"""F2 state-machine cutover: tasks.status enum -> tasks.state_id FK
(workflow_states). Backfill from the org default workflow by name
('blocked' was a placeholder -> 'todo'; blocked is now a derived
overlay, FR-3). Drop the task_status enum.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE tasks ADD COLUMN state_id uuid REFERENCES workflow_states(id) ON DELETE RESTRICT",
    # Backfill from each org's default workflow, matching state names.
    """
    UPDATE tasks t
    SET state_id = ws.id
    FROM workflow_defs wd
    JOIN workflow_states ws ON ws.workflow_id = wd.id
    WHERE wd.org_id = t.org_id
      AND wd.is_default
      AND ws.name = CASE t.status::text
                      WHEN 'blocked' THEN 'todo'
                      ELSE t.status::text
                    END
    """,
    "ALTER TABLE tasks ALTER COLUMN state_id SET NOT NULL",
    "CREATE INDEX ix_tasks_state_id ON tasks (state_id)",
    "ALTER TABLE tasks DROP COLUMN status",
    "DROP TYPE task_status",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("CREATE TYPE task_status AS ENUM ('todo', 'in_progress', 'blocked', 'done')")
    op.execute("ALTER TABLE tasks ADD COLUMN status task_status NOT NULL DEFAULT 'todo'")
    op.execute(
        """
        UPDATE tasks t
        SET status = ws.name::task_status
        FROM workflow_states ws
        WHERE ws.id = t.state_id
          AND ws.name IN ('todo', 'in_progress', 'done')
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_tasks_state_id")
    op.execute("ALTER TABLE tasks DROP COLUMN state_id")
