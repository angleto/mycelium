"""Invoice letterhead (issuer logo + header text) and per-client date format.

Two additive, nullable feature columns, no data change:

- ``issuer_profiles``: ``letterhead`` (free-text header block printed at
  the top of the courtesy PDF), ``logo_mime`` / ``logo_filename`` /
  ``logo_data`` (an optional raster logo; the bytes live in the row, a
  deferred ORM column so list/get queries never pull them).
- ``client_profile.invoice_date_format``: a closed-set pattern token
  ("YYYY-MM-DD" | "DD-MM-YYYY" | "DD/MM/YYYY" | "MM/DD/YYYY" |
  "DD.MM.YYYY"); NULL -> ISO (the historical behaviour). Courtesy-PDF
  only; the FatturaPA XML date format is unaffected.

Revision ID: 0064
Revises: 0063
Create Date: 2026-06-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("issuer_profiles", sa.Column("letterhead", sa.Text(), nullable=True))
    op.add_column("issuer_profiles", sa.Column("logo_mime", sa.String(length=64), nullable=True))
    op.add_column(
        "issuer_profiles", sa.Column("logo_filename", sa.String(length=255), nullable=True)
    )
    op.add_column("issuer_profiles", sa.Column("logo_data", sa.LargeBinary(), nullable=True))
    op.add_column(
        "client_profile", sa.Column("invoice_date_format", sa.String(length=16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("client_profile", "invoice_date_format")
    op.drop_column("issuer_profiles", "logo_data")
    op.drop_column("issuer_profiles", "logo_filename")
    op.drop_column("issuer_profiles", "logo_mime")
    op.drop_column("issuer_profiles", "letterhead")
