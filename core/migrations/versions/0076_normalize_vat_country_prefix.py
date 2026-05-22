"""Normalize stored VAT ids: lift a country prefix into IdPaese (FR-9).

``issuer_profiles.piva`` / ``client_profile.id_codice`` may hold a VIES-form
value (``IT13438810015``) with the country glued to the number, which would put
the prefix in FatturaPA ``IdCodice`` (SdI scarto). New saves are normalized in
the service layer (``flow_core.vat.normalize_vat``); this one-off cleans rows
already stored. Only values starting with two letters followed by a digit are
touched. Data-only; the downgrade is a no-op (normalization is not reversible).

Revision: 0076
Down revision: 0075
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "UPDATE issuer_profiles SET paese = upper(left(piva, 2)), "
    "piva = substring(piva from 3) WHERE piva ~ '^[A-Za-z]{2}[0-9]'",
    "UPDATE client_profile SET id_paese = upper(left(id_codice, 2)), "
    "id_codice = substring(id_codice from 3) WHERE id_codice ~ '^[A-Za-z]{2}[0-9]'",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    # Data normalization is not reversible; the schema is unchanged.
    pass
