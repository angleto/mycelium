"""Online-learning feedback on garden_classify suggestions (ADR-0037).

One append-only, org-scoped table: every accept / reject / override /
ignore the user makes on a ``garden_classify`` proposal, plus every
automatic maturity promotion the worker performs (``action='auto'``). It
is the event log the (future) learning loop projects from and the audit
trail that makes ``garden_apply`` and the auto-promotion reversible.

RLS pattern mirrors 0011 (note_part): ENABLE + FORCE row level security,
an org-predicate policy for both USING and WITH CHECK, and the
``flow_app`` grant.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "classification_feedback",
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
        # Not an FK: the event must outlive the node it describes.
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("suggestion_type", sa.String(16), nullable=False),
        sa.Column("suggestion_value", postgresql.JSONB(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("override_value", postgresql.JSONB(), nullable=True),
        sa.Column(
            "ts",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column(
            "signals_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "suggestion_type IN ('tag','link','maturity','cluster')",
            name="ck_classification_feedback_suggestion_type",
        ),
        sa.CheckConstraint(
            "action IN ('accept','reject','override','ignore','auto')",
            name="ck_classification_feedback_action",
        ),
    )
    op.create_index("ix_classification_feedback_org_id", "classification_feedback", ["org_id"])
    # The read path of the learning loop: per (org, user, type), newest first.
    op.create_index(
        "ix_classification_feedback_lookup",
        "classification_feedback",
        ["org_id", "user_id", "suggestion_type", sa.text("ts DESC")],
    )

    op.execute("ALTER TABLE classification_feedback ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE classification_feedback FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_classification_feedback ON classification_feedback "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE classification_feedback TO flow_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_classification_feedback ON classification_feedback")
    op.drop_index("ix_classification_feedback_lookup", table_name="classification_feedback")
    op.drop_index("ix_classification_feedback_org_id", table_name="classification_feedback")
    op.drop_table("classification_feedback")
