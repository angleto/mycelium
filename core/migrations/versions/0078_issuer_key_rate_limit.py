"""Per-issuer-key rate-limit bucket (task 19b7e874, phase 3b).

A shared-store (Postgres) fixed-window counter, ONE row per
``(key_id, endpoint_class)`` -- bounded, no accumulation. The check is an atomic
``INSERT ... ON CONFLICT DO UPDATE`` that resets the window or increments the
count in a single statement, and raises 429 past the class budget. org-scoped
with FORCE RLS (no cross-tenant reader), same posture as ``api_idempotency``.

Revision ID: 0078
Revises: 0077
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "issuer_key_rate_limit",
        sa.Column(
            "key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issuer_api_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint_class", sa.String(length=16), nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key_id", "endpoint_class", name="pk_issuer_key_rate_limit"),
    )
    op.execute("ALTER TABLE issuer_key_rate_limit ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE issuer_key_rate_limit FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_issuer_key_rate_limit ON issuer_key_rate_limit "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE issuer_key_rate_limit TO mycelium_app"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_issuer_key_rate_limit ON issuer_key_rate_limit")
    op.drop_table("issuer_key_rate_limit")
