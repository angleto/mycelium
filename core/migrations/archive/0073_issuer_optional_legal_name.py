"""Issuer profile: make ``legal_name`` nullable (persona fisica naming).

FatturaPA ``Anagrafica`` is a choice: ``Denominazione`` (legal entity) OR
``Nome``+``Cognome`` (persona fisica). A forfettario ditta individuale is a
persona fisica, so the AdE-issued invoice uses Nome/Cognome. The columns
``first_name``/``last_name`` already exist; this drops the NOT NULL on
``legal_name`` so a persona-fisica profile can be saved with only Nome/Cognome.
The "exactly one naming mode complete" invariant is enforced in the service /
schema (never empty in the DB by accident), not by a CHECK constraint.

Revision ID: 0073
Revises: 0072
Create Date: 2026-06-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "issuer_profiles",
        "legal_name",
        existing_type=sa.String(length=200),
        nullable=True,
    )


def downgrade() -> None:
    # A NULL legal_name (persona-fisica row) would violate the restored NOT
    # NULL: backfill from Nome+Cognome (or "") before re-imposing it.
    op.execute(
        "UPDATE issuer_profiles "
        "SET legal_name = COALESCE(legal_name, "
        "NULLIF(TRIM(CONCAT_WS(' ', first_name, last_name)), ''), '') "
        "WHERE legal_name IS NULL"
    )
    op.alter_column(
        "issuer_profiles",
        "legal_name",
        existing_type=sa.String(length=200),
        nullable=False,
    )
