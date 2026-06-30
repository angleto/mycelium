"""Per-user mycelium-QR avatar on the global users table.

Additive nullable columns: the avatar PNG bytes (a scannable vCard QR drawn
as a mycelial network), its mime, and the deterministic styling identity
(seed + the two colors) plus an updated-at for cache-busting. ``users`` is a
global table (no tenant RLS), so no policy/grant boilerplate.

Revision ID: 0072
Revises: 0071
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_data", sa.LargeBinary(), nullable=True))
    op.add_column("users", sa.Column("avatar_mime", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("avatar_seed", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("avatar_bg", sa.String(length=9), nullable=True))
    op.add_column("users", sa.Column("avatar_net", sa.String(length=9), nullable=True))
    op.add_column(
        "users",
        sa.Column("avatar_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    for col in (
        "avatar_updated_at",
        "avatar_net",
        "avatar_bg",
        "avatar_seed",
        "avatar_mime",
        "avatar_data",
    ):
        op.drop_column("users", col)
