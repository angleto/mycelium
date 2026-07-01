"""Issuer logo QR recipe (encoded vCard fields + ECC), so the logo config
restores the saved selection on reload instead of the defaults.

Two additive non-null columns (defaults preserve current behaviour: no fields
recorded, ECC H).

Revision ID: 0076
Revises: 0075
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "issuer_profiles",
        sa.Column("logo_qr_fields", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        "issuer_profiles",
        sa.Column("logo_qr_ecc", sa.String(length=1), nullable=False, server_default="H"),
    )


def downgrade() -> None:
    op.drop_column("issuer_profiles", "logo_qr_ecc")
    op.drop_column("issuer_profiles", "logo_qr_fields")
