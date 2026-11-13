"""Grant the narrow scope keys wherever the wide ones were granted.

On 2026-09-03 two scope keys were split because each did two jobs:
``workflows:write`` advanced one task AND redefined the state machine every
task runs on, ``tags:write`` filed a task AND rewrote the taxonomy. The
narrow halves became ``tasks:state`` and ``tags:assign``, and two
transitional any-of sets let an assistant granted the wide key before the
split keep advancing and filing tasks.

Those sets made the split decorative: everything kept working with the wide
key, so nothing ever moved off it. This migration grants the narrow key to
every assistant that holds the wide one without it, which is exactly the
reach the any-of sets gave it, so nobody gains or loses an operation. The
same change collapses the sets to their narrow key on REST and MCP (task
e2ef53be).

Idempotent: a row that already holds the narrow key is not touched, and
running the statements twice adds nothing.

The downgrade is a no-op on purpose. It cannot tell a narrow key this
migration added from one a person granted afterwards, and removing the
second would take away an operation somebody chose to give. Restoring the
any-of sets in code is enough to undo the behaviour.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (wide key, narrow key it was carved into). Exported for the test, which
# runs these exact statements against real assistant rows.
_SPLITS: tuple[tuple[str, str], ...] = (
    ("workflows:write", "tasks:state"),
    ("tags:write", "tags:assign"),
)

REGRANT_STATEMENTS: tuple[str, ...] = tuple(
    f"UPDATE ai_assistants SET scope = scope || '[\"{narrow}\"]'::jsonb "
    f"WHERE scope ? '{wide}' AND NOT scope ? '{narrow}'"
    for wide, narrow in _SPLITS
)


def upgrade() -> None:
    for stmt in REGRANT_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    pass
