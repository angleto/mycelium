"""ADR-0037 online learning loop: classification_personal_prior.

The per-user learned-prior store the garden classifier reads back to
re-rank its suggestions. Derived, replayable state (a projection of the
append-only ``classification_feedback`` log), so RLS reuses the per-org
story (0025 pattern) and nothing else is needed: no forbid_mutation
trigger (the loop UPDATEs ``value`` on every decision and the decay
sweep UPDATEs it nightly), WITH CHECK still pins every row to its org.

Composite PK ``(org_id, user_id, feature_key)``: one row per feature a
user has given feedback on; absent = neutral.

Revision ID: 0050
Revises: 0049
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "classification_personal_prior",
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
        sa.Column("feature_key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "org_id", "user_id", "feature_key", name="pk_classification_personal_prior"
        ),
    )
    # The decay sweep scans by (org, updated_at); the read-back loads a
    # user's whole prior set by (org, user). One index covers both prefixes.
    op.create_index(
        "ix_classification_personal_prior_org_user",
        "classification_personal_prior",
        ["org_id", "user_id"],
    )

    op.execute("ALTER TABLE classification_personal_prior ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE classification_personal_prior FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_classification_personal_prior ON classification_personal_prior "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "classification_personal_prior TO mycelium_app"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS p_classification_personal_prior ON classification_personal_prior"
    )
    op.drop_index(
        "ix_classification_personal_prior_org_user",
        table_name="classification_personal_prior",
    )
    op.drop_table("classification_personal_prior")
