"""Per-contact "show on invoice" toggles on the issuer profile.

Three additive, non-null booleans (default true) letting the issuer decide
which of its own contacts appear: ``show_phone``/``show_email`` gate the
FatturaPA Contatti block AND the courtesy PDF; ``show_pec`` gates the
PDF-only cedente PEC. Default true so a contact already emitted in the XML
today is not silently dropped by the upgrade.

Revision ID: 0071
Revises: 0070
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for col in ("show_phone", "show_email", "show_pec"):
        op.add_column(
            "issuer_profiles",
            sa.Column(col, sa.Boolean(), nullable=False, server_default=sa.text("true")),
        )


def downgrade() -> None:
    for col in ("show_pec", "show_email", "show_phone"):
        op.drop_column("issuer_profiles", col)
