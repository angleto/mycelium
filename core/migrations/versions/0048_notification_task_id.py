"""Add notifications.task_id: dispatch-time eligibility gate for reminders.

``scan_reminders`` excludes terminal / archived / soft-deleted tasks at
ENQUEUE time, but a reminder is enqueued as soon as its firing moment
enters the look-ahead window and then HELD until ``fire_at`` arrives. If
the task closes (reaches a terminal state, is archived, or is
soft-deleted) in that gap, ``dispatch_pending`` still fired it -- a
reminder for an already-finished task.

This adds the task linkage as a queryable column so dispatch can
re-validate eligibility at SEND time, instead of re-parsing it out of
``dedupe_key`` (a string) -- the same anti-pattern migration 0018 fixed
for ``fire_at``. NULL = not gated (non-reminder notifications, e.g.
coordination offers/handoffs, fire immediately by design).

ON DELETE CASCADE: a hard-deleted task drops its notifications. Existing
rows backfill ``task_id = NULL`` (the pre-0048 behaviour: never gated).

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        # Match the metadata ``naming_convention`` (fk_%(table)s_%(col)s_
        # %(referred_table)s) so the model's inline FK and this constraint
        # share a name -- otherwise autogenerate sees phantom drift.
        "fk_notifications_task_id_tasks",
        "notifications",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Dispatch gathers the task ids of pending rows to gate them in one query.
    op.create_index(
        op.f("ix_notifications_task_id"),
        "notifications",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_notifications_task_id"), table_name="notifications")
    op.drop_constraint("fk_notifications_task_id_tasks", "notifications", type_="foreignkey")
    op.drop_column("notifications", "task_id")
