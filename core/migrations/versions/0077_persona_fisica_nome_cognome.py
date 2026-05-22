"""Persona fisica Nome/Cognome on issuer + client profiles (#2, FR-9).

FatturaPA ``AnagraficaType`` is a choice: ``Denominazione`` OR ``Nome`` +
``Cognome``. Add optional Nome/Cognome (max 60 latin chars, the XSD's
``String60LatinType``) so a physical person can be represented cleanly instead
of stuffing "Nome Cognome" into Denominazione. Additive nullable columns on
``issuer_profiles`` (cedente) and ``client_profile`` (cessionario).

Revision: 0077
Down revision: 0076
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "ALTER TABLE issuer_profiles ADD COLUMN nome varchar(60)",
    "ALTER TABLE issuer_profiles ADD COLUMN cognome varchar(60)",
    "ALTER TABLE client_profile ADD COLUMN nome varchar(60)",
    "ALTER TABLE client_profile ADD COLUMN cognome varchar(60)",
)


DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE client_profile DROP COLUMN IF EXISTS cognome",
    "ALTER TABLE client_profile DROP COLUMN IF EXISTS nome",
    "ALTER TABLE issuer_profiles DROP COLUMN IF EXISTS cognome",
    "ALTER TABLE issuer_profiles DROP COLUMN IF EXISTS nome",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
