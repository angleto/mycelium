"""Issuer profile: optional RiferimentoNormativo for DatiRiepilogo (FR-9).

Free-text legal reference for the VAT exemption, emitted in
``DatiRiepilogo/RiferimentoNormativo`` for lines carrying a Natura (e.g. the
forfettario N2.2). Optional; max 100 latin chars (XSD ``String100LatinType``).
Additive nullable column on the existing ``issuer_profiles`` table.

Revision: 0075
Down revision: 0074
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE issuer_profiles ADD COLUMN riferimento_normativo varchar(100)")


def downgrade() -> None:
    op.execute("ALTER TABLE issuer_profiles DROP COLUMN IF EXISTS riferimento_normativo")
