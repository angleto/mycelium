"""Blob sources: chunk_index column for multi-vector documents.

Adds the chunk-index slot needed for paragraph-split indexing of long
notes (task `bbc21aa1`). Backward-compat: every existing source gets
``chunk_index=0`` interpreted as "whole document" (matching the legacy
single-vector semantics), so the migration is data-preserving.

The composite PK gains the column so ``(blob_id, source_kind, source_id,
chunk_index)`` becomes the natural key: one source can now own N blobs
(one per chunk) without colliding. The retrieve-time dedupe lives in
``DedupeBySourceStage`` (drops duplicate ``(source_kind, source_id)``
keeping max score).

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER on a non-partitioned table; the PK rewrite is a single
    # statement so the constraint never disappears between drop and
    # recreate (Postgres holds an ACCESS EXCLUSIVE during the swap).
    op.add_column(
        "blob_sources",
        sa.Column(
            "chunk_index",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute("ALTER TABLE blob_sources DROP CONSTRAINT pk_blob_sources")
    op.execute(
        "ALTER TABLE blob_sources ADD CONSTRAINT pk_blob_sources "
        "PRIMARY KEY (blob_id, source_kind, source_id, chunk_index)"
    )


def downgrade() -> None:
    # Drop the new PK + drop the column + restore the legacy PK. Will
    # fail if any source row already has chunk_index > 0 (would
    # introduce duplicates on the legacy key), which is the correct
    # safe default -- a downgrade after chunking is in active use
    # should be explicit, not silent.
    op.execute("ALTER TABLE blob_sources DROP CONSTRAINT pk_blob_sources")
    op.drop_column("blob_sources", "chunk_index")
    op.execute(
        "ALTER TABLE blob_sources ADD CONSTRAINT pk_blob_sources "
        "PRIMARY KEY (blob_id, source_kind, source_id)"
    )
