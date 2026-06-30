"""Generalise checklist items to a polymorphic owner (task OR note) + body.

``task_checklist_items`` now also backs note checklists (task bae178d2,
"todo list nelle note"): the owner is exactly one of ``task_id`` /
``note_id`` (enforced by an XOR check), and an optional ``body`` carries
an articulate markdown comment for an item, rendered / edited as markdown
in the shared checklist widget. The table name is kept (no rename) for
migration safety and to avoid churn across the 50+ references; it is no
longer task-only.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_checklist_items",
        sa.Column("note_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "task_checklist_items",
        sa.Column("body", sa.Text(), nullable=True),
    )
    # task_id was NOT NULL; a note-owned item has it NULL.
    op.alter_column("task_checklist_items", "task_id", nullable=True)
    op.create_foreign_key(
        "task_checklist_items_note_id_fkey",
        "task_checklist_items",
        "notes",
        ["note_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_task_checklist_items_note_id",
        "task_checklist_items",
        ["note_id"],
    )
    # Exactly one owner. ``<>`` over the two NULL-tests is true iff
    # precisely one of task_id / note_id is set.
    op.create_check_constraint(
        "ck_task_checklist_items_owner_xor",
        "task_checklist_items",
        "(task_id IS NULL) <> (note_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_task_checklist_items_owner_xor",
        "task_checklist_items",
        type_="check",
    )
    op.drop_index("ix_task_checklist_items_note_id", table_name="task_checklist_items")
    op.drop_constraint(
        "task_checklist_items_note_id_fkey",
        "task_checklist_items",
        type_="foreignkey",
    )
    # Downgrade is only safe once note-owned rows are gone (they violate
    # the restored NOT NULL); drop them first.
    op.execute("DELETE FROM task_checklist_items WHERE note_id IS NOT NULL")
    op.alter_column("task_checklist_items", "task_id", nullable=False)
    op.drop_column("task_checklist_items", "body")
    op.drop_column("task_checklist_items", "note_id")
