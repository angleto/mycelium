"""Search micro-fix bundle: FTS italian dictionary + IP distance HNSW.

Three independent improvements on the memory retrieval surface, bundled
because together they cost ~3h and each is small / revertible:

1. ``fts_lang``: a second GENERATED ``tsvector`` column over the
   ``italian`` dictionary, in addition to the existing ``simple`` ``fts``
   column. The lexical branch ORs the two so a query like "correre"
   matches "corro"/"corre" (stemmed) without losing cross-language
   matching that ``simple`` still provides.

2. HNSW index migration ``vector_cosine_ops -> vector_ip_ops``. Our
   embedders (e5, bge) emit L2-normalized vectors, so inner product is
   mathematically equivalent to cosine but ~10% faster on HNSW (no
   norm division). The retrieve query switches to
   ``max_inner_product``.

3. ``hnsw.iterative_scan`` (pgvector >=0.7) is a session-local GUC the
   retrieve flips on; not a schema change, listed here only for the
   migration audit trail. The actual ``SET LOCAL`` lives in
   ``services.memory.retrieve``.

memory_blobs is PARTITION BY HASH (org_id, 8 partitions). PG 16+
propagates a ``CREATE INDEX`` on the parent to every partition and
attaches them; same for ``ADD COLUMN``. We drop+recreate the embedding
index globally; the parent ``CASCADE`` cleans the per-partition
indexes, the new ``CREATE INDEX`` rebuilds them.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) Italian-stemmed FTS column. ``simple`` stays so cross-language
    #    matching keeps working (e.g. an English query against an
    #    Italian doc still finds shared tokens like "task", "email").
    op.execute(
        "ALTER TABLE memory_blobs "
        "ADD COLUMN fts_lang tsvector "
        "GENERATED ALWAYS AS (to_tsvector('italian', COALESCE(text, ''))) STORED"
    )
    op.execute("CREATE INDEX ix_memory_blobs_fts_lang ON memory_blobs USING gin (fts_lang)")

    # 2) HNSW embedding index: cosine_ops -> ip_ops. Vectors are
    #    L2-normalized by the embedder (asserted at write time in
    #    services.memory._safe_embed), so cosine and IP rank identically
    #    but IP skips the per-pair norm computation.
    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_embedding")
    op.execute(
        "CREATE INDEX ix_memory_blobs_embedding "
        "ON memory_blobs USING hnsw (embedding vector_ip_ops)"
    )


def downgrade() -> None:
    # Revert to cosine_ops + drop the italian column.
    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_embedding")
    op.execute(
        "CREATE INDEX ix_memory_blobs_embedding "
        "ON memory_blobs USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_fts_lang")
    op.execute("ALTER TABLE memory_blobs DROP COLUMN IF EXISTS fts_lang")
