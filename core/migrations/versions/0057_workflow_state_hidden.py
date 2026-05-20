"""workflow_states.is_hidden: kanban-only visibility hint.

A workflow state may be marked hidden (e.g. a "blocked" or "deferred"
state the operator doesn't want cluttering the board). The SPA's
kanban view skips columns whose state is hidden by default, behind a
"Show hidden" toggle. Storage-level boolean only — transitions /
dispatch / scheduler don't read it; a task in a hidden state is still
a full citizen of the workflow.

Revision: 0057
Down revision: 0056
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "ALTER TABLE workflow_states "
    "ADD COLUMN is_hidden boolean NOT NULL DEFAULT false",
)


DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE workflow_states DROP COLUMN IF EXISTS is_hidden",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
