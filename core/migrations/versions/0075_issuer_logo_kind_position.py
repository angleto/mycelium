"""Issuer logo kind + position (avatar / image / avatar+QR, and placement).

Two additive non-null columns (defaults preserve current behaviour: a plain
uploaded image, positioned left of the letterhead title). ``logo_kind`` also
drives the courtesy-PDF logo box size (a scannable QR needs a bigger square
than the 58x22 landscape band).

Revision ID: 0075
Revises: 0074
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "issuer_profiles",
        sa.Column("logo_kind", sa.String(length=16), nullable=False, server_default="image"),
    )
    op.add_column(
        "issuer_profiles",
        sa.Column("logo_position", sa.String(length=8), nullable=False, server_default="left"),
    )


def downgrade() -> None:
    op.drop_column("issuer_profiles", "logo_position")
    op.drop_column("issuer_profiles", "logo_kind")
