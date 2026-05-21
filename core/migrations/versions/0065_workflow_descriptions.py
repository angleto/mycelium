"""Add ``description`` column on workflow_definitions and
workflow_states. The SPA + MCP can now surface the operator's free-
form explanation of "what this workflow is for" and "what this state
means", so an AI assistant called via MCP can reason about a task's
state without guessing from the state name.

Revision: 0065
Down revision: 0064
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "ALTER TABLE workflow_defs ADD COLUMN description text",
    "ALTER TABLE workflow_states ADD COLUMN description text",
)


DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE workflow_states DROP COLUMN IF EXISTS description",
    "ALTER TABLE workflow_defs DROP COLUMN IF EXISTS description",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
