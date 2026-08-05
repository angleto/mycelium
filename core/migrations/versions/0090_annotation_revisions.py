"""Recovery history for comments: ``entity_kind='annotation'``.

Tasks and notes have had a revision timeline since migration 0006;
comments never did. A comment is the third markdown document in the
model -- ``annotations`` already addresses a ``note_part`` and a
``task_description`` through one handle -- so an edit to a task's work
diary was the only body write in the system that left no recoverable
trace: ``version`` said something changed, and nothing said what.

The table is reused as-is; this widens the polymorphic domain:

- ``ck_entity_revision_entity_kind`` gains ``'annotation'``;
- ``trg_comment_revision_cascade`` mirrors the task/note cascade so a
  purged comment takes its snapshots with it. Postgres has no
  polymorphic FK, so this trigger IS the referential integrity -- and
  the only thing standing between a hard-deleted comment and its text
  living on in the timeline.

Revision ID: 0090
Revises: 0089
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0090"
down_revision: str | None = "0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_entity_revision_entity_kind", "entity_revision", type_="check")
    op.create_check_constraint(
        "ck_entity_revision_entity_kind",
        "entity_revision",
        "entity_kind IN ('task','note','annotation')",
    )
    # Same function the task/note triggers use; it reads the kind from
    # its trigger argument.
    op.execute(
        "CREATE TRIGGER trg_comment_revision_cascade "
        "AFTER DELETE ON comments "
        "FOR EACH ROW EXECUTE FUNCTION entity_revision_cascade('annotation')"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_comment_revision_cascade ON comments")
    # Rows of the widened kind would violate the narrowed constraint.
    op.execute("DELETE FROM entity_revision WHERE entity_kind = 'annotation'")
    op.drop_constraint("ck_entity_revision_entity_kind", "entity_revision", type_="check")
    op.create_check_constraint(
        "ck_entity_revision_entity_kind",
        "entity_revision",
        "entity_kind IN ('task','note')",
    )
