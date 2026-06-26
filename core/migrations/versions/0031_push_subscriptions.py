"""Web Push channel: 'webpush' enum value + push_subscriptions table.

Adds the ``webpush`` value to the ``notification_channel`` enum and a
table of browser push subscriptions (one row per user+device endpoint).
Org-scoped with FORCE RLS like every tenant table. The pref
(``notification_prefs`` channel='webpush') is the on/off switch; the
reminder dispatcher fans a webpush notification out to all of a user's
subscriptions here.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    # New channel value. IF NOT EXISTS keeps it idempotent; a straight
    # ADD VALUE works inside Alembic's transaction on PG 12+ (migration
    # 0002 does the same). The value is not USED in this migration (the
    # new table has no channel column), so the same-transaction-use caveat
    # does not apply.
    op.execute("ALTER TYPE notification_channel ADD VALUE IF NOT EXISTS 'webpush'")

    op.create_table(
        "push_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.String(length=2048), nullable=False),
        sa.Column("p256dh", sa.String(length=256), nullable=False),
        sa.Column("auth", sa.String(length=256), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", "endpoint", name="uq_push_subscriptions_org_endpoint"),
    )
    op.create_index("ix_push_subscriptions_org_id", "push_subscriptions", ["org_id"])
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])

    op.execute("ALTER TABLE push_subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE push_subscriptions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_push_subscriptions ON push_subscriptions "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE push_subscriptions TO mycelium_app")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_push_subscriptions ON push_subscriptions")
    op.drop_table("push_subscriptions")
    # The 'webpush' enum value is intentionally left in place: PostgreSQL
    # cannot drop an enum value, and removing it would break any pref or
    # notification row that referenced it.
