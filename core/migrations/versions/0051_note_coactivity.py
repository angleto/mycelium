"""note_coactivity: materialised co-activity edges (task f0a15247).

ADR-0031 v2+ third edge-weight source. The offline worker aggregates the
activity log into pairwise co-activity session counts; the read side
(``services/graph.compute_note_edge_weights``) folds them into the
soft-OR. One row per canonical undirected note pair, RLS-scoped by org
(0025 pattern), note FKs cascade so a hard-deleted note drops its edges.

Also adds a composite ``(org_id, entity, ts)`` index on activity_log:
the co-activity aggregation (and the existing garden_health / note
conflict reads) all filter org + entity + time window, which today rides
the org-only index. Additive, no behaviour change.

Revision ID: 0051
Revises: 0050
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "note_coactivity",
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
            "note_a_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "note_b_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_coactive_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", "note_a_id", "note_b_id", name="uq_note_coactivity_pair"),
    )
    op.create_index("ix_note_coactivity_org_id", "note_coactivity", ["org_id"])

    op.execute("ALTER TABLE note_coactivity ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE note_coactivity FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_note_coactivity ON note_coactivity "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE note_coactivity TO flow_app")

    # Supporting index for org + entity + time-window scans on the
    # activity log (co-activity aggregation, garden_health, note conflict).
    op.create_index(
        "ix_activity_log_org_entity_ts",
        "activity_log",
        ["org_id", "entity", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_activity_log_org_entity_ts", table_name="activity_log")
    op.execute("DROP POLICY IF EXISTS p_note_coactivity ON note_coactivity")
    op.drop_index("ix_note_coactivity_org_id", table_name="note_coactivity")
    op.drop_table("note_coactivity")
