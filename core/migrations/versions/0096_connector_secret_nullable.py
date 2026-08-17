"""A connector must be creatable BEFORE its provider has issued a secret.

The setup order is not a preference, it is forced by the provider: the webhook
URL contains the connector id, so the connector has to exist first; the
provider only shows a signing secret once that URL is registered as an
endpoint. Requiring the secret at creation therefore closed a circle with no
entry point -- to create the connector you needed a secret you could not obtain
without the URL that creation produces. The only way through was to register a
throwaway URL at the provider, harvest the secret, create the connector and go
back to fix the URL, which is not something an operator should have to invent.

So ``signing_secret_ciphertext`` becomes nullable and "exists, cannot verify
yet" is an explicit state. Nothing is loosened: minting a secret for a vendor
provider is still refused (a secret the provider never issued makes a connector
that looks healthy and rejects every delivery), the ingress treats a missing
secret as a refusal exactly like a bad signature, and ENABLING is refused until
a real secret is installed -- which is the state that matters, because it is the
only one in which the endpoint accepts money events.

Revision ID: 0096
Revises: 0095
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "payment_connectors",
        "signing_secret_ciphertext",
        existing_type=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    # A connector still waiting for its secret cannot survive the narrowing:
    # there is no value to invent that would be correct, and inventing one is
    # precisely the failure this design refuses. Such a connector is disabled
    # by construction (enabling requires a secret), so it has never received
    # anything and dropping it loses no fiscal record.
    op.execute("DELETE FROM payment_connectors WHERE signing_secret_ciphertext IS NULL")
    op.alter_column(
        "payment_connectors",
        "signing_secret_ciphertext",
        existing_type=sa.Text(),
        nullable=False,
    )
