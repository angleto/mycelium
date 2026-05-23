"""Rename ``task_assignees`` -> ``task_collaborators`` (ADR-0028 D2
follow-up).

After ADR-0028, the singular "assignee" of a task lives on
``tasks.assignee_id`` (a FK into ``identities``). The legacy M:N table
narrowed to "everyone else who collaborates on the task"; the rename
makes the intent visible at the schema level. PK / FK / indexes are
renamed accordingly.

Revision: 0090
Down revision: 0089
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0090"
down_revision: str | None = "0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "ALTER TABLE task_assignees RENAME TO task_collaborators",
    # Postgres renames the PK automatically; the indexes carry their
    # old names, so rename them to match the new convention.
    "ALTER INDEX IF EXISTS ix_task_assignees_org_id RENAME TO ix_task_collaborators_org_id",
    # The policy attached to the table follows the rename automatically;
    # rename it explicitly for grep-ability.
    "ALTER POLICY p_task_assignees ON task_collaborators RENAME TO p_task_collaborators",
)


DOWNGRADE: tuple[str, ...] = (
    "ALTER POLICY p_task_collaborators ON task_collaborators RENAME TO p_task_assignees",
    "ALTER INDEX IF EXISTS ix_task_collaborators_org_id RENAME TO ix_task_assignees_org_id",
    "ALTER TABLE task_collaborators RENAME TO task_assignees",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
