"""Composite (org_id, due_date) + (org_id, start_date) indexes on tasks.

Task 39e98a30 adds date-window filters and a date sort to ``list_tasks``
(due_on / due_before / due_after / start_*; order_by=due_date|start_date).
These two composite indexes let the org-scoped date predicates and the
"soonest due first" sort be served from an index instead of a full
per-org table scan as the task table grows. Additive, no data change.

Revision ID: 0063
Revises: 0062
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_tasks_org_due_date", "tasks", ["org_id", "due_date"])
    op.create_index("ix_tasks_org_start_date", "tasks", ["org_id", "start_date"])


def downgrade() -> None:
    op.drop_index("ix_tasks_org_start_date", table_name="tasks")
    op.drop_index("ix_tasks_org_due_date", table_name="tasks")
