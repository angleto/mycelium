"""kg_entity + kg_edge -- temporal knowledge graph (ADR-0044, Track B).

Typed entity nodes (resolved/deduped per org) + bi-temporal relation facts.
kg_edge carries valid-time (valid_from/valid_to) and transaction-time
(created_at .. invalidated_at); a contradiction sets invalidated_at +
superseded_by_edge_id instead of deleting (invalidate-not-delete), enforced
by a BEFORE UPDATE trigger that freezes a row once invalidated. Facts are
born review_state='proposed' for autonomous extraction (ADR-0043 gating).
RLS per-org FORCE (0006 pattern). predicate is an OPEN vocabulary (no CHECK);
entity_type is a closed set (CHECK).

Revision ID: 0067
Revises: 0066
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"

_ENTITY_TYPES = (
    "person",
    "organization",
    "project",
    "place",
    "product",
    "event",
    "concept",
    "other",
)


def upgrade() -> None:
    op.create_table(
        "kg_entity",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("normalized_name", sa.String(length=512), nullable=False),
        sa.Column(
            "aliases",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("origin_model_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
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
            "entity_type IN ('" + "', '".join(_ENTITY_TYPES) + "')",
            name="ck_kg_entity_entity_type",
        ),
    )
    op.create_index("ix_kg_entity_org_id", "kg_entity", ["org_id"])
    op.create_index("ix_kg_entity_org_norm", "kg_entity", ["org_id", "normalized_name"])
    # One canonical entity per (org, type, normalized name) -- the dedupe key.
    op.create_index(
        "uq_kg_entity_org_type_norm",
        "kg_entity",
        ["org_id", "entity_type", "normalized_name"],
        unique=True,
    )

    op.create_table(
        "kg_edge",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kg_entity.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "object_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kg_entity.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("predicate", sa.String(length=64), nullable=False),
        sa.Column("valid_from", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "invalidated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "superseded_by_edge_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kg_edge.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("review_state", sa.String(length=16), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("origin_model_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_note_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
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
            "valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from",
            name="ck_kg_edge_valid_window",
        ),
        sa.CheckConstraint("subject_id <> object_id", name="ck_kg_edge_no_self"),
    )
    op.create_index("ix_kg_edge_org_id", "kg_edge", ["org_id"])
    # At most one CURRENT fact per typed triple (superseding facts coexist as
    # invalidated history) -- the invalidate-not-delete slot.
    op.create_index(
        "uq_kg_edge_current",
        "kg_edge",
        ["org_id", "subject_id", "predicate", "object_id"],
        unique=True,
        postgresql_where=sa.text("invalidated_at IS NULL"),
    )
    op.create_index(
        "ix_kg_edge_subject_current",
        "kg_edge",
        ["org_id", "subject_id"],
        postgresql_where=sa.text("invalidated_at IS NULL"),
    )
    op.create_index(
        "ix_kg_edge_object_current",
        "kg_edge",
        ["org_id", "object_id"],
        postgresql_where=sa.text("invalidated_at IS NULL"),
    )
    op.create_index(
        "ix_kg_edge_review_proposed",
        "kg_edge",
        ["org_id", "created_at"],
        postgresql_where=sa.text("review_state = 'proposed'"),
    )

    # Invalidate-not-delete: once a fact is invalidated (tombstoned) it is
    # frozen history -- no path may rewrite it. The live row may still
    # transition (set invalidated_at/valid_to/review_state). Mirrors the
    # entity_revision sealed-immutable trigger (migration 0006).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kg_edge_no_update_invalidated()
        RETURNS TRIGGER AS $$
        BEGIN
          IF OLD.invalidated_at IS NOT NULL THEN
            RAISE EXCEPTION
              'kg_edge % is invalidated history and cannot be updated', OLD.id
              USING ERRCODE = 'check_violation';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_kg_edge_no_update_invalidated "
        "BEFORE UPDATE ON kg_edge "
        "FOR EACH ROW EXECUTE FUNCTION kg_edge_no_update_invalidated()"
    )

    for table in ("kg_entity", "kg_edge"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY p_{table} ON {table} USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO mycelium_app")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_kg_edge_no_update_invalidated ON kg_edge")
    op.execute("DROP FUNCTION IF EXISTS kg_edge_no_update_invalidated()")
    for table in ("kg_edge", "kg_entity"):
        op.execute(f"DROP POLICY IF EXISTS p_{table} ON {table}")
    op.drop_table("kg_edge")
    op.drop_table("kg_entity")
