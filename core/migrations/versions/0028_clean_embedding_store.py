"""Two-tier embedding store + per-org embedder provider (task 5276207e).

Replaces the v1(384)+v2(1024) migration scaffold with TWO permanent,
purpose-built tiers fused at search time (RRF):

- LOCAL  ``memory_blobs.embedding`` vector(1024): bge-m3, always-on
  rank-0 fallback (offline/OSS dense search). 1024 = bge-m3 native,
  under pgvector's 2000-dim HNSW ceiling for ``vector``.
- HOSTED ``memory_blobs.embedding_hosted`` halfvec(4000): per-org
  Scaleway, selected via ``org_embedder_provider``. 4000 = pgvector's
  HNSW ceiling for ``halfvec``, so any future model up to 4000 native
  fits (Matryoshka truncation) with no reindex.

Embeddings are derived from ``memory_blobs.text`` and re-embeddable, so
dropping the old vectors loses nothing recoverable: the backfill worker
rebuilds them. Note/task primary data is untouched (only vector columns
change). ``adjudication_steps.embedding`` (debate strategy, local
embedder, no index) is reconciled to vector(1024) too.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIM = 1024
_DIM_HOSTED = 4000
_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    # --- memory_blobs: collapse v1(384) + v2(1024) into one embedding(1024).
    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_embedding")
    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_embedding_v2")
    op.execute(
        "ALTER TABLE memory_blobs "
        "DROP COLUMN IF EXISTS embedding, "
        "DROP COLUMN IF EXISTS embedding_v2, "
        "DROP COLUMN IF EXISTS model_id_v2, "
        "DROP COLUMN IF EXISTS dim_v2"
    )
    op.execute(f"ALTER TABLE memory_blobs ADD COLUMN embedding vector({_DIM}) NULL")
    op.execute(f"ALTER TABLE memory_blobs ALTER COLUMN dim SET DEFAULT {_DIM}")
    # HNSW on the partitioned parent propagates to all partitions (PG16+).
    op.execute(
        "CREATE INDEX ix_memory_blobs_embedding "
        "ON memory_blobs USING hnsw (embedding vector_ip_ops)"
    )

    # --- hosted tier: halfvec(4000) (ceiling for the halfvec HNSW opclass).
    op.execute(
        f"ALTER TABLE memory_blobs "
        f"ADD COLUMN embedding_hosted halfvec({_DIM_HOSTED}) NULL, "
        f"ADD COLUMN model_id_hosted varchar(160) NULL, "
        f"ADD COLUMN dim_hosted integer NULL"
    )
    op.execute(
        "CREATE INDEX ix_memory_blobs_embedding_hosted "
        "ON memory_blobs USING hnsw (embedding_hosted halfvec_ip_ops)"
    )

    # --- adjudication_steps: 384 -> 1024 (no vector index on this table).
    op.execute("ALTER TABLE adjudication_steps DROP COLUMN IF EXISTS embedding")
    op.execute(f"ALTER TABLE adjudication_steps ADD COLUMN embedding vector({_DIM}) NULL")

    # --- org_embedder_provider: per-org hosted embedder (mirrors 0026/0027).
    op.create_table(
        "org_embedder_provider",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="local"),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("base_url", sa.String(length=400), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "provider IN ('local', 'scaleway')",
            name="ck_org_embedder_provider_kind",
        ),
    )
    op.execute("ALTER TABLE org_embedder_provider ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE org_embedder_provider FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_org_embedder_provider ON org_embedder_provider "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE org_embedder_provider TO flow_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_org_embedder_provider ON org_embedder_provider")
    op.drop_table("org_embedder_provider")

    op.execute("ALTER TABLE adjudication_steps DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE adjudication_steps ADD COLUMN embedding vector(384) NULL")

    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_embedding_hosted")
    op.execute(
        "ALTER TABLE memory_blobs "
        "DROP COLUMN IF EXISTS embedding_hosted, "
        "DROP COLUMN IF EXISTS model_id_hosted, "
        "DROP COLUMN IF EXISTS dim_hosted"
    )

    op.execute("DROP INDEX IF EXISTS ix_memory_blobs_embedding")
    op.execute("ALTER TABLE memory_blobs DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE memory_blobs ALTER COLUMN dim SET DEFAULT 384")
    op.execute("ALTER TABLE memory_blobs ADD COLUMN embedding vector(384) NULL")
    op.execute(
        "CREATE INDEX ix_memory_blobs_embedding "
        "ON memory_blobs USING hnsw (embedding vector_ip_ops)"
    )
    # Restore the v2 dual-column scaffold.
    op.execute(
        "ALTER TABLE memory_blobs "
        "ADD COLUMN embedding_v2 vector(1024) NULL, "
        "ADD COLUMN model_id_v2 varchar(160) NULL, "
        "ADD COLUMN dim_v2 integer NULL"
    )
    op.execute(
        "CREATE INDEX ix_memory_blobs_embedding_v2 "
        "ON memory_blobs USING hnsw (embedding_v2 vector_ip_ops)"
    )
