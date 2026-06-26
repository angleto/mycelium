"""Search-result click log: the data source of ADR-0035 ``recall_at_k``.

Append-only event table: one row per search-result click (query ->
clicked entity at rank N of M shown). ``is_probe`` flags synthetic
golden-fixture probes so the sensor reads real queries only. Org-scoped
with FORCE RLS like every tenant event table (same shape as
``classification_feedback``); the clicked entity is deliberately not an
FK so the event survives the entity's deletion.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "search_clicks",
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
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("query", sa.String(500), nullable=False),
        sa.Column("hit_kind", sa.String(16), nullable=False),
        # Not an FK: the event must outlive the entity it describes.
        sa.Column("hit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("is_probe", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "ts",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "hit_kind IN ('task','note','blob')",
            name="ck_search_clicks_hit_kind",
        ),
        sa.CheckConstraint("rank >= 1", name="ck_search_clicks_rank"),
        sa.CheckConstraint("result_count >= rank", name="ck_search_clicks_result_count"),
    )
    op.create_index("ix_search_clicks_org_id", "search_clicks", ["org_id"])
    # The recall sensor's read path: per-org trailing window, newest first.
    op.create_index(
        "ix_search_clicks_org_ts",
        "search_clicks",
        ["org_id", sa.text("ts DESC")],
    )

    op.execute("ALTER TABLE search_clicks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE search_clicks FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_search_clicks ON search_clicks "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE search_clicks TO mycelium_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_search_clicks ON search_clicks")
    op.drop_index("ix_search_clicks_org_ts", table_name="search_clicks")
    op.drop_index("ix_search_clicks_org_id", table_name="search_clicks")
    op.drop_table("search_clicks")
