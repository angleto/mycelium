"""email_account_default_tags (WS-1, per-account default tags).

A flat set of tags (typ. client + project) auto-applied to everything
ingested from an email account: memory blobs on the 'email' channel and
email->task/note. Association table mirroring ``memory_blob_tags``;
cascades on account or tag delete. RLS per-org (0025 pattern).

Revision ID: 0060
Revises: 0059
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "email_account_default_tags",
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("email_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tag_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("account_id", "tag_id", name="pk_email_account_default_tags"),
    )
    op.create_index(
        "ix_email_account_default_tags_account_id",
        "email_account_default_tags",
        ["account_id"],
    )
    op.create_index(
        "ix_email_account_default_tags_org_id",
        "email_account_default_tags",
        ["org_id"],
    )
    op.create_index(
        "ix_email_account_default_tags_tag_id",
        "email_account_default_tags",
        ["tag_id"],
    )

    op.execute("ALTER TABLE email_account_default_tags ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE email_account_default_tags FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_email_account_default_tags ON email_account_default_tags "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE email_account_default_tags TO mycelium_app"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_email_account_default_tags ON email_account_default_tags")
    op.drop_index(
        "ix_email_account_default_tags_tag_id",
        table_name="email_account_default_tags",
    )
    op.drop_index(
        "ix_email_account_default_tags_org_id",
        table_name="email_account_default_tags",
    )
    op.drop_index(
        "ix_email_account_default_tags_account_id",
        table_name="email_account_default_tags",
    )
    op.drop_table("email_account_default_tags")
