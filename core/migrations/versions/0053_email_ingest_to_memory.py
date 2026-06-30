"""email ingest-to-memory: per-account opt-in + per-message bulk flag.

Task 2a901dee. ``email_accounts.ingest_to_memory`` gates whether synced
messages flow into the 'email' memory channel (OFF by default).
``email_messages.is_bulk`` records the upstream hygiene decision (list /
bulk / auto-submitted) so the ingest filter is a cheap column read.

Revision ID: 0053
Revises: 0052
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "email_accounts",
        sa.Column(
            "ingest_to_memory",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "email_messages",
        sa.Column(
            "is_bulk",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("email_messages", "is_bulk")
    op.drop_column("email_accounts", "ingest_to_memory")
