"""Let a row opt out of automatic search indexing.

Every task and every note reaches ``memory_blobs`` because it exists, not
because anyone asked: the indexer renders title plus body and writes a blob
with ``project_id=None`` for tasks ("so the task hit is org-wide"), and from
there the text answers ``memory_search`` and the unified ``/search`` for any
actor in the org. There was no opt-out at all -- no column, no argument, no
flag -- so material that should not be recalled had only one remedy, deleting
the row.

``index_scope='none'`` is that opt-out and only that. It is NOT a read
boundary: ``get_task`` selects on the primary key with no actor predicate, the
RLS policies carry ``org_id`` as their only term, and the server-side ILIKE of
``list_tasks(q=...)`` / ``list_notes(q=...)`` reads the live columns without
touching ``memory_blobs``. What the column closes is unrequested recall; a
deliberate query by an actor who can already read the row is untouched, and
closing that would be a per-actor read predicate, which this is not.

The column sits on ``notes`` rather than on ``note_part`` although the part is
the indexed unit: a new part of a scoped-out note must be born scoped out too,
and a per-part column would give it the ``'org'`` server default and silently
re-index the note.

No backfill. The default preserves today's behaviour for every existing row,
and nothing sweeps retroactively: the only remedy path is an explicit flip,
row by row, which deletes that row's blobs.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE TYPE index_scope AS ENUM ('org', 'none')")
    # ADD COLUMN with a non-volatile default does not rewrite the table on
    # PostgreSQL 11+, so this stays a catalogue-only change on both tables.
    for table in ("tasks", "notes"):
        op.add_column(
            table,
            sa.Column(
                "index_scope",
                postgresql.ENUM("org", "none", name="index_scope", create_type=False),
                nullable=False,
                server_default="org",
            ),
        )


def downgrade() -> None:
    op.drop_column("notes", "index_scope")
    op.drop_column("tasks", "index_scope")
    op.execute("DROP TYPE index_scope")
