"""Link a note to a task (a task's work note).

A task can have a single linked "work note" you open and write in;
time spent on the note is billed to the task (time entries are already
task-scoped, no new model). The link is a nullable ``notes.task_id``
FK; existing notes stay NULL (no backfill). ``ON DELETE SET NULL`` so
deleting/erasing a task keeps the note (it just loses the link).

``notes`` is already org-scoped + RLS (the link is same-org, enforced
in the service). A plain column add inherits the table's existing RLS
policy and flow_app grants, so no extra GRANT is needed (same as the
0029/0030 column-add migrations).

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE notes ADD COLUMN IF NOT EXISTS task_id uuid "
    "REFERENCES tasks(id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_notes_task_id ON notes (task_id)",
)

DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_notes_task_id",
    "ALTER TABLE notes DROP COLUMN IF EXISTS task_id",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
