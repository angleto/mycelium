"""Global system_settings singleton: runtime SdI environment switch.

A single-row, non-RLS, globally-scoped settings table (like refresh_tokens:
granted directly to ``mycelium_app``). ``sdi_environment`` ('test' |
'production') selects which configured endpoint URL the live RiceviFile send
targets, so an admin can flip test<->production from Settings WITHOUT a redeploy
(the env var only provides the two URLs; the active one is chosen here at
runtime). Defaults to 'test' so a fresh deploy never sends a real invoice by
accident. Singleton enforced by a boolean PK pinned TRUE.

Revision ID: 0074
Revises: 0073
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sdi_environment", sa.String(length=16), server_default="test", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id IS TRUE", name="system_settings_singleton"),
        sa.CheckConstraint(
            "sdi_environment IN ('test', 'production')", name="system_settings_sdi_env"
        ),
    )
    # Non-RLS global table: grant the runtime role directly (the app reads the
    # active environment in invoice.transmit and the admin router writes it).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE system_settings TO mycelium_app")
    # Seed the singleton row, defaulting to the safe (test) environment.
    op.execute("INSERT INTO system_settings (id, sdi_environment) VALUES (true, 'test')")


def downgrade() -> None:
    op.drop_table("system_settings")
