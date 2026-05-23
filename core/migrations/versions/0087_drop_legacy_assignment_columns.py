"""Drop legacy task.executor_user_id + task.assignee_handle.

Closes the Stage C refactor of #21 (docs/adr/0028). The Stage B
mirror columns are no longer read by any code path: ``assignee_id``
+ ``identities`` are the single source of truth for the "who"
addressing, and ``owner_id`` is the accountability axis.

``executor_kind`` SURVIVES as the fallback routing hint for tasks
without an assignee (see ``task.py`` doc comment); it is no longer
a duplicate of the resolved identity's kind but a stand-alone
default.

Revision: 0087
Down revision: 0086
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0087"
down_revision: str | None = "0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "ALTER TABLE tasks DROP COLUMN IF EXISTS executor_user_id",
    "ALTER TABLE tasks DROP COLUMN IF EXISTS assignee_handle",
)


DOWNGRADE: tuple[str, ...] = (
    # NULL values are acceptable for the legacy columns; the data
    # cannot be reconstructed losslessly from the identities table
    # anyway (an identity row deletion would have already cleared
    # the corresponding ``assignee_id`` via ON DELETE SET NULL).
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assignee_handle varchar(40)",
    (
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS executor_user_id uuid "
        "REFERENCES users(id) ON DELETE SET NULL"
    ),
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
