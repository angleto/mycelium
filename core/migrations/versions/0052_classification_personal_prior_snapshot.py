"""classification_personal_prior_snapshot (ADR-0037 follow-up, ea2156df).

Daily checkpoint of a user's learned priors so rollback is decay-aware
point-in-time (rebuild_from_feedback alone is forward-only on decay) and
drift can be measured against a real "30 days ago" baseline. One row per
(org, user) per snapshot; blob = {feature_key: value}. RLS per-org (0025
pattern), user_id cascades.

Revision ID: 0052
Revises: 0051
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "classification_personal_prior_snapshot",
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
        sa.Column(
            "snapshot_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("blob", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_cpp_snapshot_org_user_at",
        "classification_personal_prior_snapshot",
        ["org_id", "user_id", "snapshot_at"],
    )

    op.execute("ALTER TABLE classification_personal_prior_snapshot ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE classification_personal_prior_snapshot FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_classification_personal_prior_snapshot "
        "ON classification_personal_prior_snapshot "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON TABLE classification_personal_prior_snapshot TO mycelium_app"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS p_classification_personal_prior_snapshot "
        "ON classification_personal_prior_snapshot"
    )
    op.drop_index(
        "ix_cpp_snapshot_org_user_at",
        table_name="classification_personal_prior_snapshot",
    )
    op.drop_table("classification_personal_prior_snapshot")
