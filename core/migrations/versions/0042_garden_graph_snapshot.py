"""Materialised graph-analytics snapshot (task d8664631).

One row per org: PageRank, betweenness (offline-only, the worker is
the sole producer), Leiden clusters + modularity, with the input
signature that gates recomputation. Org-scoped FORCE RLS like the
other tenant tables.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "garden_graph_snapshot",
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
        sa.Column("signature", sa.String(256), nullable=False),
        sa.Column("centrality", postgresql.JSONB(), nullable=False),
        sa.Column("betweenness", postgresql.JSONB(), nullable=False),
        sa.Column("clusters", postgresql.JSONB(), nullable=False),
        sa.Column("modularity", sa.Float(), nullable=True),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", name="uq_garden_graph_snapshot_org"),
    )
    op.create_index("ix_garden_graph_snapshot_org_id", "garden_graph_snapshot", ["org_id"])

    op.execute("ALTER TABLE garden_graph_snapshot ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE garden_graph_snapshot FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_garden_graph_snapshot ON garden_graph_snapshot "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE garden_graph_snapshot TO flow_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_garden_graph_snapshot ON garden_graph_snapshot")
    op.drop_index("ix_garden_graph_snapshot_org_id", table_name="garden_graph_snapshot")
    op.drop_table("garden_graph_snapshot")
