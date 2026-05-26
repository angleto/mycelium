"""Refresh tokens: rotating, family-tracked, reuse-detection.

The access JWT stays short-lived (``jwt_ttl_seconds``, default 1h);
this table backs the long-lived refresh credential the SPA presents
to ``/auth/refresh`` to mint a new pair without re-prompting the
user. Each rotation marks the prior row ``used_at`` and points
``replaced_by_id`` at its successor, so a replayed refresh is
detectable as ``used_at IS NOT NULL`` and triggers full-family
revocation (token-theft signal). ``family_id`` lets a single logout
revoke every descendant of a login session in one update.

Global table (mirrors ``revoked_tokens`` / ``email_verification_tokens``):
consumed pre-tenant, no RLS, granted directly to ``flow_app``.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "family_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "replaced_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("refresh_tokens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        sa.Index("ix_refresh_tokens_user_id", "user_id"),
        sa.Index("ix_refresh_tokens_family_id", "family_id"),
        sa.Index("ix_refresh_tokens_expires_at", "expires_at"),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE refresh_tokens TO flow_app")


def downgrade() -> None:
    op.drop_table("refresh_tokens")
