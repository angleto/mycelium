"""The accredited channel's fiscal code moves into platform configuration.

It lived only in ``MYCELIUM_SDI_INTERMEDIARY_ID_CODICE``, so correcting it
meant editing a k8s ConfigMap and rolling the deployment. That is the wrong
home for it twice over: an operator cannot see what the running process holds
(a sibling setting was demanded by the fail-closed boot check for months while
being read by nothing, ADR-0053), and the value has a real reason to change --
FatturaPA 1.1.1.2 wants the CODICE FISCALE of the trasmittente and SdI verifies
it in Anagrafe Tributaria as such, so a deployment configured with an 11-digit
P.IVA of a physical person is scartata 00300 on every file the moment it
transmits for a tenant other than itself.

Nullable-by-default rather than seeded, deliberately: the migrate Job receives
only ``MYCELIUM_DATABASE_URL_SYNC`` and never sees the ConfigMap, so a
seed-from-env here would silently write an empty string and take the
trasmittente away. The resolver falls back to the env value while the column is
blank, which makes this expand-only: nothing breaks on deploy, the operator
sets the value from Settings, and the ConfigMap line can then go.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 28 characters is CodiceType's maximum in the official schema; an Italian
    # value is 11 digits or a 16-character codice fiscale. Empty means "not set
    # here", which is what makes the env fallback reachable.
    op.add_column(
        "system_settings",
        sa.Column(
            "sdi_intermediary_id_codice",
            sa.String(length=28),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("system_settings", "sdi_intermediary_id_codice")
