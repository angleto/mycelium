"""F6 (additive): hierarchical memory on pgvector (docs/adr/0005,
0007, 0016, FR-8). Extends memory_blobs with the embedding, a
generated FTS column + trigram index for the lexical branch, the
access-score tiering signals and a cluster reference; adds
``blob_sources`` (N:1 provenance for GDPR erasure). HNSW + GIN indexes
are created on the partitioned table (propagated to each partition).

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"

# Fixed embedding dimension for v1 (docs/adr/0005: re-embedding to a
# different model/dim is a new column, not an in-place change).
_DIM = 384

UPGRADE: tuple[str, ...] = (
    f"ALTER TABLE memory_blobs ADD COLUMN embedding vector({_DIM})",
    "ALTER TABLE memory_blobs ADD COLUMN model_id varchar(160)",
    f"ALTER TABLE memory_blobs ADD COLUMN dim integer NOT NULL DEFAULT {_DIM}",
    "ALTER TABLE memory_blobs ADD COLUMN summary text",
    "ALTER TABLE memory_blobs ADD COLUMN access_count integer NOT NULL DEFAULT 0",
    "ALTER TABLE memory_blobs ADD COLUMN last_accessed_at timestamptz",
    "ALTER TABLE memory_blobs ADD COLUMN importance numeric(6, 4) NOT NULL DEFAULT 0",
    "ALTER TABLE memory_blobs ADD COLUMN access_score numeric(12, 6) NOT NULL DEFAULT 0",
    "ALTER TABLE memory_blobs ADD COLUMN cluster_id uuid",
    """
    ALTER TABLE memory_blobs ADD COLUMN fts tsvector
      GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED
    """,
    "CREATE INDEX ix_memory_blobs_fts ON memory_blobs USING gin (fts)",
    "CREATE INDEX ix_memory_blobs_trgm ON memory_blobs USING gin (text gin_trgm_ops)",
    "CREATE INDEX ix_memory_blobs_cluster ON memory_blobs (cluster_id)",
    """
    CREATE INDEX ix_memory_blobs_embedding ON memory_blobs
      USING hnsw (embedding vector_cosine_ops)
    """,
    """
    CREATE TABLE blob_sources (
      blob_id uuid NOT NULL,
      org_id uuid NOT NULL,
      source_kind varchar(40) NOT NULL,
      source_id varchar(255) NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_blob_sources
        PRIMARY KEY (blob_id, source_kind, source_id),
      CONSTRAINT fk_blob_sources_blob_id_memory_blobs
        FOREIGN KEY (blob_id, org_id)
        REFERENCES memory_blobs (id, org_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_blob_sources_org_id ON blob_sources (org_id)",
    "ALTER TABLE blob_sources ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE blob_sources FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_blob_sources ON blob_sources "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON blob_sources TO flow_app",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS blob_sources CASCADE")
    for idx in (
        "ix_memory_blobs_embedding",
        "ix_memory_blobs_trgm",
        "ix_memory_blobs_fts",
        "ix_memory_blobs_cluster",
    ):
        op.execute(f"DROP INDEX IF EXISTS {idx}")
    for col in (
        "fts",
        "cluster_id",
        "access_score",
        "importance",
        "last_accessed_at",
        "access_count",
        "summary",
        "dim",
        "model_id",
        "embedding",
    ):
        op.execute(f"ALTER TABLE memory_blobs DROP COLUMN IF EXISTS {col}")
