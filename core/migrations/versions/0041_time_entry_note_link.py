"""Bidirectional note <-> time-entry link + note/memo column rename.

Proposal A: a note is the work log of exactly one task, and time billed
from that note still rolls up to the task (billing is task-scoped:
``time_entries.task_id`` stays NOT NULL; rate/billable live on the
client via task -> project -> client).

- ``time_entries.note_id`` is a NULLABLE FK to ``notes(id)`` with
  ``ON DELETE SET NULL``: deleting a note must NOT delete billed time;
  the entry keeps its ``task_id`` so the invoice is unaffected, it just
  loses the note provenance.
- ``time_entries.note`` -> ``memo``: the free-text memo on a time entry
  collided confusingly with the Note entity (and the new ``note_id``).
  Renaming removes the ambiguity. Pure rename, no data change.

``time_entries`` is already org-scoped + RLS; a plain column add /
rename inherits the table's existing RLS policy and flow_app grants, so
no extra GRANT is needed (same as the 0029/0030/0037 column-add
migrations). Downgrade reverses both, symmetrically.

Revision ID: 0041
Revises: 0040
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE time_entries ADD COLUMN note_id uuid REFERENCES notes(id) ON DELETE SET NULL",
    "CREATE INDEX ix_time_entries_note_id ON time_entries (note_id)",
    "ALTER TABLE time_entries RENAME COLUMN note TO memo",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE time_entries RENAME COLUMN memo TO note",
    "DROP INDEX IF EXISTS ix_time_entries_note_id",
    "ALTER TABLE time_entries DROP COLUMN IF EXISTS note_id",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
