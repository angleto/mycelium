"""classification_jobs (ADR-0042 D5, b8c60940 / WS-D2).

On-create classification queue: create_note/create_task enqueue a pending
row in their own transaction; the garden worker drains it (classify_node +
cache in precomputed_suggestions). node_id polymorphic (note|task), no FK.
status pending -> done | error. RLS per-org (0025 pattern).

Revision ID: 0055
Revises: 0054
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "classification_jobs",
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
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_classification_job_org_status_created",
        "classification_jobs",
        ["org_id", "status", "created_at"],
    )

    op.execute("ALTER TABLE classification_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE classification_jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_classification_jobs ON classification_jobs "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE classification_jobs TO flow_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_classification_jobs ON classification_jobs")
    op.drop_index(
        "ix_classification_job_org_status_created",
        table_name="classification_jobs",
    )
    op.drop_table("classification_jobs")
