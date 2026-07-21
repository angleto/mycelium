"""First-class provenance columns on memory_blobs (enabler A, ADR-0049/ADR-0028).

Before this, a memory blob's author was only a tag-lane convention
(``agent/<handle>``), which cannot satisfy the unconditional-provenance axiom
once more than one agent writes to the shared store. Adds ``created_by`` (the
authoring identity, a user OR an ai_assistant) and ``origin_model_id`` (the LLM
that produced the text; NULL for a human author), mirroring the note / kg
provenance pattern.

``memory_blobs`` is ``PARTITION BY HASH (org_id)`` with FORCE RLS from the
baseline. Adding NULLABLE columns to the partitioned parent propagates to all
partitions as a metadata-only change (no table rewrite), and the FK + the
inherited ``p_memory_blobs`` RLS policy + grants apply to the partitions
automatically, so no new policy or grant is needed. The partial index over
non-null authors backs the provenance-filtered recall.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0085"
down_revision: str | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_blobs",
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "memory_blobs",
        sa.Column("origin_model_id", sa.String(length=128), nullable=True),
    )
    # Provenance-filtered recall queries ``created_by = <identity>``; most rows
    # are NULL (human / legacy), so a partial index keeps it small.
    op.create_index(
        "ix_memory_blobs_org_created_by",
        "memory_blobs",
        ["org_id", "created_by"],
        postgresql_where=sa.text("created_by IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_memory_blobs_org_created_by", table_name="memory_blobs")
    op.drop_column("memory_blobs", "origin_model_id")
    op.drop_column("memory_blobs", "created_by")
