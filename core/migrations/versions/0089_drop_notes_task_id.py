"""Drop the legacy ``notes.task_id`` column (docs/adr/0029 P3).

By this migration every reader and writer of the column has been
ported to the typed ``note_task_link`` table (kind='artifact' for
the Proposal A pattern). The column drop closes the refactor.

Reversible: downgrade re-adds the column nullable with a SET NULL
FK; it does NOT backfill data from ``note_task_link`` (the
information is not lossless on the way back -- a note may now have
multiple artifact links).

Revision: 0089
Down revision: 0088
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0089"
down_revision: str | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = ("ALTER TABLE notes DROP COLUMN IF EXISTS task_id",)


DOWNGRADE: tuple[str, ...] = (
    (
        "ALTER TABLE notes ADD COLUMN IF NOT EXISTS task_id uuid "
        "REFERENCES tasks(id) ON DELETE SET NULL"
    ),
    "CREATE INDEX IF NOT EXISTS ix_notes_task_id ON notes (task_id)",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
