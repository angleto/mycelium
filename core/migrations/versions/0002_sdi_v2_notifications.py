"""SdI v2 notifications: audit-log tables + denormalized verdict columns.

Adds ``invoice_notifications`` (active cycle audit: RC/MC/NS/AT/NE/DT) and
``received_invoice_notifications`` (receiver cycle audit: MT/SE/DT + EC out)
as append-only logs of every SdI message we receive (or send, for EC). Each
``(invoice_id, kind, message_id)`` row is unique so SdI retries are
idempotent on insert.

Denormalized verdict columns on ``invoices`` / ``received_invoices`` carry
the latest derived state for query-fast paths (buyer accepted/rejected /
deemed-accepted via DT timeout). They are kept in sync by the service layer
on each notification ingest.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Active-cycle notification kinds (transmitter receives).
_INV_NOTIF_KINDS = ("RC", "MC", "NS", "AT", "NE", "DT")
# Receiver-cycle notification kinds (we are the cessionario).
_RECV_NOTIF_KINDS = ("MT", "SE", "DT", "EC")
# Verdict states: 'none' until SdI relays a buyer/committente outcome.
_VERDICT_VALUES = ("none", "accepted", "rejected", "deemed_accepted")


def upgrade() -> None:
    # --- invoice_notifications -------------------------------------------------
    op.create_table(
        "invoice_notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.String(2),
            nullable=False,
        ),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("nome_file", sa.String(120), nullable=True),
        sa.Column("message_id", sa.String(14), nullable=True),
        sa.Column("raw_xml", sa.LargeBinary, nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('" + "','".join(_INV_NOTIF_KINDS) + "')",
            name="kind_chk",
        ),
        sa.Index("ix_invoice_notifications_invoice_id", "invoice_id"),
        sa.Index("ix_invoice_notifications_org_id", "org_id"),
        sa.Index(
            "uq_invoice_notifications_dedupe",
            "invoice_id",
            "kind",
            "message_id",
            unique=True,
        ),
    )
    op.execute("ALTER TABLE invoice_notifications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invoice_notifications FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_invoice_notifications ON invoice_notifications "
        "USING (org_id = (NULLIF(current_setting('app.current_org', true), ''))::uuid) "
        "WITH CHECK (org_id = (NULLIF(current_setting('app.current_org', true), ''))::uuid)"
    )

    # --- received_invoice_notifications ---------------------------------------
    op.create_table(
        "received_invoice_notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "received_invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("received_invoices.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(2), nullable=False),
        # 'in' = SdI to us; 'out' = we built and sent (EC outbound).
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("nome_file", sa.String(120), nullable=True),
        sa.Column("message_id", sa.String(14), nullable=True),
        sa.Column("raw_xml", sa.LargeBinary, nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('" + "','".join(_RECV_NOTIF_KINDS) + "')",
            name="kind_chk",
        ),
        sa.CheckConstraint(
            "direction IN ('in', 'out')",
            name="direction_chk",
        ),
        sa.Index(
            "ix_received_invoice_notifications_received_invoice_id",
            "received_invoice_id",
        ),
        sa.Index("ix_received_invoice_notifications_org_id", "org_id"),
        sa.Index(
            "uq_received_invoice_notifications_dedupe",
            "received_invoice_id",
            "kind",
            "direction",
            "message_id",
            unique=True,
        ),
    )
    op.execute("ALTER TABLE received_invoice_notifications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE received_invoice_notifications FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_received_invoice_notifications ON received_invoice_notifications "
        "USING (org_id = (NULLIF(current_setting('app.current_org', true), ''))::uuid) "
        "WITH CHECK (org_id = (NULLIF(current_setting('app.current_org', true), ''))::uuid)"
    )

    # --- denormalized verdict columns ------------------------------------------
    op.add_column(
        "invoices",
        sa.Column(
            "buyer_verdict",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "buyer_verdict_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "dt_received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "buyer_verdict_chk",
        "invoices",
        "buyer_verdict IN ('" + "','".join(_VERDICT_VALUES) + "')",
    )

    op.add_column(
        "received_invoices",
        sa.Column(
            "committente_verdict",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'none'"),
        ),
    )
    op.add_column(
        "received_invoices",
        sa.Column(
            "committente_verdict_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "received_invoices",
        sa.Column(
            "dt_received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "committente_verdict_chk",
        "received_invoices",
        "committente_verdict IN ('" + "','".join(_VERDICT_VALUES) + "')",
    )


def downgrade() -> None:
    # Constraint names below are the *suffix* the naming convention
    # ``ck_%(table_name)s_%(constraint_name)s`` interpolates into; alembic
    # rebuilds the full name on drop_constraint.
    op.drop_constraint("committente_verdict_chk", "received_invoices", type_="check")
    op.drop_column("received_invoices", "dt_received_at")
    op.drop_column("received_invoices", "committente_verdict_at")
    op.drop_column("received_invoices", "committente_verdict")

    op.drop_constraint("buyer_verdict_chk", "invoices", type_="check")
    op.drop_column("invoices", "dt_received_at")
    op.drop_column("invoices", "buyer_verdict_at")
    op.drop_column("invoices", "buyer_verdict")

    op.execute(
        "DROP POLICY IF EXISTS p_received_invoice_notifications ON received_invoice_notifications"
    )
    op.drop_table("received_invoice_notifications")

    op.execute("DROP POLICY IF EXISTS p_invoice_notifications ON invoice_notifications")
    op.drop_table("invoice_notifications")
