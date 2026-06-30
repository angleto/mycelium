"""kg_entity_source -- N:M entity provenance for GDPR erase (ADR-0044 follow-up).

Adversarial verification of the 0068 GDPR fix found a residual: kg_entity has
no provenance column, so an entity extracted into the LLM's entities[] but never
used in a relation (no kg_edge) survived a note's GDPR erase with its name
intact. This adds the provenance link (mirroring blob_sources) so
``kg.erase_by_source`` can delete an entity left with zero provenance AND zero
facts. RLS per-org FORCE; both FKs ON DELETE CASCADE.

Revision ID: 0069
Revises: 0068
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0069"
down_revision: str | None = "0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "kg_entity_source",
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
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kg_entity.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_note_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_kg_entity_source_org_id", "kg_entity_source", ["org_id"])
    # One provenance link per (entity, source note) -- idempotent re-extraction.
    op.create_index(
        "uq_kg_entity_source",
        "kg_entity_source",
        ["org_id", "entity_id", "source_note_id"],
        unique=True,
    )
    # erase-by-note (drop a note's links) and orphan-count (per entity).
    op.create_index("ix_kg_entity_source_note", "kg_entity_source", ["org_id", "source_note_id"])
    op.create_index("ix_kg_entity_source_entity", "kg_entity_source", ["org_id", "entity_id"])

    op.execute("ALTER TABLE kg_entity_source ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kg_entity_source FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_kg_entity_source ON kg_entity_source "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kg_entity_source TO mycelium_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_kg_entity_source ON kg_entity_source")
    op.drop_table("kg_entity_source")
