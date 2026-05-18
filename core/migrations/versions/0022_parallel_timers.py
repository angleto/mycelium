"""Parallel + serial time tracking.

A timer is ``serial`` (parallel = false, the default classic timer:
at most one running per user, starting one stops the previous) or
``parallel`` (parallel = true: any number run concurrently, e.g. LLM
tasks). The old "one running per (org,user)" unique index is replaced
by: one running *serial* per (org,user), and one running per
(org,user,task) so the same task is never double-tracked.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE time_entries "
    "ADD COLUMN IF NOT EXISTS parallel boolean NOT NULL DEFAULT false",
    "DROP INDEX IF EXISTS uq_time_entries_running",
    """
    CREATE UNIQUE INDEX uq_time_entries_running_serial
      ON time_entries (org_id, user_id)
      WHERE ended_at IS NULL AND parallel = false
    """,
    """
    CREATE UNIQUE INDEX uq_time_entries_running_task
      ON time_entries (org_id, user_id, task_id)
      WHERE ended_at IS NULL
    """,
)

DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS uq_time_entries_running_task",
    "DROP INDEX IF EXISTS uq_time_entries_running_serial",
    """
    CREATE UNIQUE INDEX uq_time_entries_running
      ON time_entries (org_id, user_id)
      WHERE ended_at IS NULL
    """,
    "ALTER TABLE time_entries DROP COLUMN IF EXISTS parallel",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
