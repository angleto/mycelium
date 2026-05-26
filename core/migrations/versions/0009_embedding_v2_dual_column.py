"""Embedding migration dual-column: add embedding_v2 / model_id_v2 / dim_v2.

Foundation for migrating between embedding models (task `1d081395`,
target bge-m3 1024d). pgvector doesn't support ALTER TYPE on the
vector dim, and a big-bang re-embed would block writes for hours on
large workspaces. The dual-column pattern is the standard answer:

- Old columns (``embedding``, ``model_id``, ``dim``) stay populated
  for legacy rows. The HNSW index on them keeps serving retrievals.
- New columns (``embedding_v2``, ``model_id_v2``, ``dim_v2``) start
  NULL; new writes populate v2 when ``FLOW_EMBED_MODEL_V2`` is set,
  the embedding-migration worker backfills v2 for legacy rows
  gradually, and ``memory.retrieve`` reads v2 if non-NULL else v1
  (transparent dual-read).

When coverage of v2 reaches 100%, a separate cutover migration
(``0NNN_embedding_cutover``) drops the v1 columns and renames v2 to
the canonical names. That migration is NOT part of this PR -- it's
an operational decision (when to switch) and depends on the worker
finishing its sweep.

The v2 column dimension is parameterised via ``FLOW_EMBED_DIM_V2``
(default 1024 for bge-m3). The HNSW index uses ``vector_ip_ops`` to
match the v1 op class (we already moved to IP in migration 0007); the
embedder contract still guarantees L2-normalised vectors.

memory_blobs is PARTITION BY HASH (org_id, 8 partitions). PG 16+
propagates ADD COLUMN + CREATE INDEX on the parent to every partition
automatically.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The migration is parameterised: the new dim is read from env at
# upgrade time. Default targets bge-m3 (1024d). Changing this after
# the migration ran requires another schema migration (vector dim is
# immutable in pgvector).
_DIM_V2 = int(os.environ.get("FLOW_EMBED_DIM_V2", "1024"))


def upgrade() -> None:
    # Three new columns, all NULL by default. Production rows stay
    # readable (retrieve falls back to v1); only the migration worker
    # + new writes populate v2.
    op.execute(
        f"ALTER TABLE memory_blobs "
        f"ADD COLUMN embedding_v2 vector({_DIM_V2}) NULL, "
        f"ADD COLUMN model_id_v2 varchar(160) NULL, "
        f"ADD COLUMN dim_v2 integer NULL"
    )
    op.execute(
        "CREATE INDEX ix_memory_blobs_embedding_v2 "
        "ON memory_blobs USING hnsw (embedding_v2 vector_ip_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_embedding_v2")
    op.execute(
        "ALTER TABLE memory_blobs "
        "DROP COLUMN IF EXISTS dim_v2, "
        "DROP COLUMN IF EXISTS model_id_v2, "
        "DROP COLUMN IF EXISTS embedding_v2"
    )
