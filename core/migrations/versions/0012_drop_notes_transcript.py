"""Drop ``notes.transcript`` (parent task c0459c4b, Phase 6 final =
task 1cd8bc0a, design note 2d228758).

The canonical note body now lives in ``note_part(ord=0)+`` rows
(migration 0011). Every reader was flipped first (services.notes
.get_body / _bodies_by_note, API/MCP _note derive transcript from
parts at serialise time, CLI consumes the same derived field).
Writers (create_note, update_note, transcribe, append_to_note_field,
restore_revision) now route into ``_upsert_part_zero``. Phase 6 prep
(commit ca54fe3) seeded the part(ord=0) for every text-bearing note
created post-deploy; migration 0011 backfilled pre-existing rows.

This migration removes the column without a compatibility view --
option B from the design note. Downgrade re-adds an empty column;
notes created after the drop only have part data, so a downgrade is
a one-way ticket back to ``transcript IS NULL`` for those rows
(parts data is intact, the column is just empty until a manual
re-backfill).

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("notes", "transcript")


def downgrade() -> None:
    # Re-add the column as nullable text; pre-existing notes need a
    # one-shot backfill from note_part(ord=0).body if a roll-back is
    # ever needed.
    op.add_column("notes", sa.Column("transcript", sa.Text(), nullable=True))
    op.execute(
        "UPDATE notes n SET transcript = np.body "
        "  FROM note_part np "
        " WHERE np.note_id = n.id AND np.ord = 0"
    )
