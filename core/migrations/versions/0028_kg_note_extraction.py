"""What the knowledge graph has already read.

The autonomous extraction pass sends a note's body to a metered model and
writes what it finds as proposed facts. Without a record of what it has
already read, every pass would read every note again: the facts dedupe on
their triple, so the graph would not grow wrong, but the bill would grow
every tick. A note that yields no fact leaves no trace in ``kg_edge`` or
``kg_entity_source``, so neither table can answer "already read" either.

``kg_note_extraction``  one row per note, rewritten on each attempt.
    ``parts_signature`` hashes the text of the note's parts in order: it
    changes on any edit, addition, removal or reordering of a part, and a
    pass reads a note again only when it has.
    ``outcome`` is ``ok``, ``failed`` (retried until ``attempts`` reaches
    the pass's limit) or ``too_large`` (the note alone exceeds what one pass
    may spend; it comes back after an edit, or once the per-pass budget is
    raised above its ``estimated_tokens``, instead of starving every note
    behind it). Workspace RLS on ``org_id``; the note cascades, so
    a purged note leaves no row behind.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    # The foreign keys lock notes and organizations in SHARE ROW EXCLUSIVE;
    # same bound and reason as 0026/0027: a failed migration is retried, a
    # stalled notes table is an outage.
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.create_table(
        "kg_note_extraction",
        sa.Column(
            "id", PG_UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("note_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("parts_signature", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("facts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kg_note_extraction"),
        sa.CheckConstraint(
            "outcome IN ('ok', 'failed', 'too_large')", name="ck_kg_note_extraction_outcome"
        ),
        sa.ForeignKeyConstraint(
            ["note_id"],
            ["notes.id"],
            name="fk_kg_note_extraction_note_id_notes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_kg_note_extraction_org_id_organizations",
            ondelete="CASCADE",
        ),
    )
    # The model inherits ``index=True`` on org_id from OrgScopedMixin, like
    # every tenant table; created here so the schema matches the model.
    op.create_index("ix_kg_note_extraction_org_id", "kg_note_extraction", ["org_id"])
    op.create_index(
        "uq_kg_note_extraction_note",
        "kg_note_extraction",
        ["org_id", "note_id"],
        unique=True,
    )
    op.execute("ALTER TABLE kg_note_extraction ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE kg_note_extraction FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_kg_note_extraction ON kg_note_extraction "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    create_owner_session_policy(op.get_bind(), "kg_note_extraction")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kg_note_extraction TO mycelium_app")


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE kg_note_extraction FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS owner_session_all ON kg_note_extraction")
    op.execute("DROP POLICY IF EXISTS p_kg_note_extraction ON kg_note_extraction")
    op.drop_index("uq_kg_note_extraction_note", table_name="kg_note_extraction")
    op.drop_index("ix_kg_note_extraction_org_id", table_name="kg_note_extraction")
    op.drop_table("kg_note_extraction")
