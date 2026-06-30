"""Garden health daily snapshots (ADR-0035).

One row per ``(org, day)``: the structural symbiosis metrics computed by
the nightly worker tick, kept as a time-series for the dashboard
sparklines. The live ``GET /garden/health`` endpoint computes current
values directly; this table is the history the sparkline reads.

RLS pattern mirrors 0021 (classification_feedback): ENABLE + FORCE row
level security, an org-predicate policy for both USING and WITH CHECK,
and the ``mycelium_app`` grant.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "garden_health_daily",
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
        sa.Column("day", sa.Date(), nullable=False),
        # The full metric set as computed that day: {key: {value, floor,
        # reason}}. JSONB so adding a metric never needs a migration.
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # One snapshot per day: the nightly tick upserts on this.
        sa.UniqueConstraint("org_id", "day", name="uq_garden_health_daily_org_day"),
    )
    # Read path: latest-first within an org (current value + 30d sparkline).
    op.create_index(
        "ix_garden_health_daily_org_day",
        "garden_health_daily",
        ["org_id", sa.text("day DESC")],
    )

    op.execute("ALTER TABLE garden_health_daily ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE garden_health_daily FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_garden_health_daily ON garden_health_daily "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE garden_health_daily TO mycelium_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_garden_health_daily ON garden_health_daily")
    op.drop_index("ix_garden_health_daily_org_day", table_name="garden_health_daily")
    op.drop_table("garden_health_daily")
