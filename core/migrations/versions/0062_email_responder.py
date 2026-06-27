"""email responder (WS-4): per-account opt-in + job queue.

``email_accounts.auto_draft_replies`` opts an account into the autonomous
responder; ``email_responder_jobs`` is the queue the worker drains to draft
a reply per new message (withheld in state 'drafted' until a human
approves). RLS per-org (0025 pattern); enqueue + review run in a tenant
session and the worker claims per-org.

Revision ID: 0062
Revises: 0061
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.add_column(
        "email_accounts",
        sa.Column(
            "auto_draft_replies",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_table(
        "email_responder_jobs",
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
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("email_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("draft_reply", sa.Text(), nullable=True),
        sa.Column("origin_model_id", sa.String(length=128), nullable=True),
        sa.Column("sent_id", sa.String(length=998), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("message_id", name="uq_email_responder_jobs_message_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'drafted', 'sent', 'rejected', 'failed')",
            name="ck_email_responder_jobs_status",
        ),
    )
    op.create_index(
        "ix_email_responder_jobs_pending",
        "email_responder_jobs",
        ["org_id", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.execute("ALTER TABLE email_responder_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE email_responder_jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_email_responder_jobs ON email_responder_jobs "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE email_responder_jobs TO mycelium_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_email_responder_jobs ON email_responder_jobs")
    op.drop_index("ix_email_responder_jobs_pending", table_name="email_responder_jobs")
    op.drop_table("email_responder_jobs")
    op.drop_column("email_accounts", "auto_draft_replies")
