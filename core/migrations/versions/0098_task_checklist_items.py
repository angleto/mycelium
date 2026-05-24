"""Add ``task_checklist_items``: lightweight ticked items inside a task.

Not sub-tasks (no parent_task_id on ``tasks``, no discriminator). The
widget is a second tab next to the markdown description in the SPA
task view, and its sole purpose is to give the task a simple checklist
("shopping list" idiom) without dragging in the rest of the task
machinery (billing, scheduling, time-tracking).

RLS posture matches the post-#48 default: ENABLE, not FORCE. The table
is touched only via tenant sessions; no SECURITY DEFINER reads.

Revision: 0098
Down revision: 0097
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0098"
down_revision: str | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"


UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE task_checklist_items (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      text text NOT NULL,
      done boolean NOT NULL DEFAULT false,
      position integer NOT NULL DEFAULT 0,
      done_at timestamptz,
      done_by uuid REFERENCES users(id) ON DELETE SET NULL,
      created_by uuid REFERENCES users(id) ON DELETE SET NULL,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_task_checklist_items_text_nonempty
        CHECK (length(btrim(text)) > 0)
    )
    """,
    # Primary access path: list a task's items ordered by position.
    "CREATE INDEX ix_task_checklist_items_task_id_position "
    "ON task_checklist_items (task_id, position)",
    "CREATE INDEX ix_task_checklist_items_org_id ON task_checklist_items (org_id)",
    "ALTER TABLE task_checklist_items ENABLE ROW LEVEL SECURITY",
    (
        f"CREATE POLICY p_task_checklist_items ON task_checklist_items "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON task_checklist_items TO flow_app",
)


DOWNGRADE: tuple[str, ...] = ("DROP TABLE IF EXISTS task_checklist_items CASCADE",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
