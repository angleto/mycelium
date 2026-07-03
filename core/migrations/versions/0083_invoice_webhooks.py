"""Signed outbound webhooks on invoice state changes (task 2c23e955, ADR-0047).

Two org-scoped FORCE-RLS tables:
- webhook_endpoints: per-issuer subscription + Fernet-enveloped signing secret.
- webhook_deliveries: transactional-outbox row per (event, endpoint), frozen
  payload snapshot, delivery state + lease for at-least-once worker delivery.

Revision ID: 0083
Revises: 0082
Create Date: 2026-07-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY p_{table} ON {table} USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO mycelium_app")


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issuer_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("previous_secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("previous_secret_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "event_types",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint(
            "length(name) >= 1 AND length(name) <= 120", name="ck_webhook_endpoints_name_len"
        ),
    )
    op.create_index(
        "ix_webhook_endpoints_issuer_profile_id", "webhook_endpoints", ["issuer_profile_id"]
    )
    op.create_index(
        "uq_webhook_endpoints_active_url",
        "webhook_endpoints",
        ["issuer_profile_id", "url"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    _rls("webhook_endpoints")

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "endpoint_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issuer_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issuer_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "payload_schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_excerpt", sa.String(length=512), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
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
        sa.UniqueConstraint("endpoint_id", "dedupe_key", name="uq_webhook_deliveries_dedupe"),
        sa.CheckConstraint(
            "status IN ('pending','delivering','delivered','failed','dead')",
            name="ck_webhook_deliveries_status",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('pending','failed')"),
    )
    op.create_index(
        "ix_webhook_deliveries_delivering",
        "webhook_deliveries",
        ["last_attempt_at"],
        postgresql_where=sa.text("status = 'delivering'"),
    )
    op.create_index("ix_webhook_deliveries_invoice", "webhook_deliveries", ["invoice_id"])
    op.create_index("ix_webhook_deliveries_created_at", "webhook_deliveries", ["created_at"])
    _rls("webhook_deliveries")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_webhook_deliveries ON webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.execute("DROP POLICY IF EXISTS p_webhook_endpoints ON webhook_endpoints")
    op.drop_table("webhook_endpoints")
