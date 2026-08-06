"""Index ``users`` on the admin list's sort key.

``GET /admin/users`` used to select every row; it now returns one
bounded page ordered by ``(created_at DESC, id DESC)``. The baseline
schema indexes ``users`` on the primary key and on ``email`` only
(``0001_baseline.sql``), so without this every page would sort the whole
table to return fifty rows -- the fix would bound the RESPONSE while
leaving the QUERY linear in the user count, which is the half that
actually degrades as the table grows.

Both columns descend, matching the query exactly: PostgreSQL can walk an
index backwards, but a composite whose direction disagrees with the sort
forces a sort node anyway.

Revision ID: 0091
Revises: 0090
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0091"
down_revision: str | None = "0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_created_at_id ON users (created_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_created_at_id")
