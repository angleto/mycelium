"""Optional FatturaPA NumeroCivico on issuer and client profiles.

One additive, nullable column per table, no data change. The civic number
is emitted in the FatturaPA Sede block (between Indirizzo and CAP) when set;
it is purely additive to the civic number a user may already keep inline in
the address line. XSD NumeroCivico is max 8 chars, hence VARCHAR(8).

Revision ID: 0070
Revises: 0069
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("issuer_profiles", sa.Column("civic_number", sa.String(length=8), nullable=True))
    op.add_column("client_profile", sa.Column("civic_number", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("client_profile", "civic_number")
    op.drop_column("issuer_profiles", "civic_number")
