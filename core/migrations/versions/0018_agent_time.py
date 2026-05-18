"""Snapshot the task executor on time entries (additive).

AI-tracked time must be distinguishable and never summed into a
human's totals. ``time_entries.executor_kind`` snapshots the task's
ExecKind (human|llm_agent) at start; the enum type ``exec_kind``
already exists (task migration). Reports can filter by it.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE time_entries "
        "ADD COLUMN executor_kind exec_kind NOT NULL DEFAULT 'human'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE time_entries DROP COLUMN IF EXISTS executor_kind")
