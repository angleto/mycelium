"""precomputed_suggestions (ADR-0042 D4, b8c60940 / WS-D2).

Cache of a node's ``classify_node`` proposals, written by the on-create
classification job so suggestions are ready at open time instead of
recomputed live on every open. One row per proposed item; ``node_id`` is
polymorphic (note | task) so it carries no FK. RLS per-org (0025 pattern).
A recompute deletes + rewrites a node's rows (a cache, not a log).

Revision ID: 0054
Revises: 0053
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "precomputed_suggestions",
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
        sa.Column("node_kind", sa.String(length=16), nullable=False),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("suggestion_type", sa.String(length=32), nullable=False),
        sa.Column("suggestion_value", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("rationale", sa.String(), nullable=True),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_precomputed_suggestion_org_node",
        "precomputed_suggestions",
        ["org_id", "node_id"],
    )

    op.execute("ALTER TABLE precomputed_suggestions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE precomputed_suggestions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_precomputed_suggestions ON precomputed_suggestions "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE precomputed_suggestions TO flow_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_precomputed_suggestions ON precomputed_suggestions")
    op.drop_index(
        "ix_precomputed_suggestion_org_node",
        table_name="precomputed_suggestions",
    )
    op.drop_table("precomputed_suggestions")
