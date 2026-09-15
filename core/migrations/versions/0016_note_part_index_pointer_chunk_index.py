"""A note part points at the SET of blobs that index it, not at one blob.

``note_part_index_pointer`` had ``PRIMARY KEY (part_id)`` and its own
docstring said what that meant: "UNIQUE(blob_id) keeps the binding
strictly 1:1". The indexer honoured it by embedding the whole part in one
call, so a part longer than the embedder's 2048-token window was
represented semantically by its head while reading as fully indexed, and
nothing declared the loss (task 11154e32). The longest part in the
reference workspace is 166 KB and had one vector.

The fix is to chunk the part, and chunking is not a change of code alone:
with the primary key as it stands, chunks 1..N-1 have nowhere to be
recorded. They would exist as blobs that no maintenance path reaches,
which is worse than the defect being repaired, and no constraint would
fire to say so. So the cardinality moves first.

``chunk_index`` NOT NULL DEFAULT 0, primary key recreated on
``(part_id, chunk_index)``. Every existing row is chunk 0 by
construction, so the default settles them in place: no data backfill
here. Re-chunking the long parts that are already indexed as one blob is
a separate, idempotent pass in the note-search backfill sweep, because it
has to re-embed and this migration must not.

TWO THINGS DELIBERATELY UNTOUCHED.

``uq_note_part_index_pointer_blob_id`` stays. The direction that has to
stop being 1:1 is part -> blob; blob -> part stays exactly 1:1, one blob
per chunk, and half a dozen resolvers walk it that way (unified search,
focus context, edge usage, graph proximity). Dropping the unique would
break them all to express nothing.

The composite ``ON DELETE CASCADE`` to ``memory_blobs`` stays, and with N
rows it is what makes the whole pointer set vanish with the blob set
instead of leaving rows behind for a part that is no longer indexed.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "note_part_index_pointer",
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint("pk_note_part_index_pointer", "note_part_index_pointer", type_="primary")
    op.create_primary_key(
        "pk_note_part_index_pointer",
        "note_part_index_pointer",
        ["part_id", "chunk_index"],
    )


def downgrade() -> None:
    # Reversing the cardinality means choosing which chunk survives, and
    # only chunk 0 can: it is the one the old shape would have held. The
    # rest go, together with the blobs they point at (the pointer's FK
    # does not reach the blob, so the blobs are deleted explicitly first
    # -- leaving them would orphan them on the note channel, retrievable
    # and unmaintained, which is the exact defect 0016 exists to end).
    op.execute(
        """
        DELETE FROM memory_blobs b
         USING note_part_index_pointer p
         WHERE p.blob_id = b.id
           AND p.org_id = b.org_id
           AND p.chunk_index <> 0
        """
    )
    op.execute("DELETE FROM note_part_index_pointer WHERE chunk_index <> 0")
    op.drop_constraint("pk_note_part_index_pointer", "note_part_index_pointer", type_="primary")
    op.create_primary_key("pk_note_part_index_pointer", "note_part_index_pointer", ["part_id"])
    op.drop_column("note_part_index_pointer", "chunk_index")
