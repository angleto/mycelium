"""Add users.language: per-user UI/notification locale ("it" | "en").

Worker-generated reminder text (``scan_reminders``) has no request
context, so it cannot read the SPA's ``Accept-Language`` header; it needs
a stored preference to render the title/body in the recipient's language.
NULL = the default locale ("en"). Captured from the SPA's language
switcher (mirrors the ``timezone`` capture).

Revision ID: 0034
Revises: 0033
Create Date: 2026-06-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("language", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "language")
