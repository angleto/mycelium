"""ADR-0036 event bus: event_outbox + per-executor event quotas.

The coordinated read/write substrate for multi-agent operation on the
graph (tasks cb6d6baf / c19b5489). An ``event_outbox`` row is written in
the SAME transaction as the originating mutation (authoritative state); a
deferred trigger ``pg_notify('flow.event', id)`` at COMMIT lets future
subscribers pull the row by id (the 8 KB NOTIFY cap is why only the id
travels). RLS reuses the per-org story (0025 pattern), so the bus needs
no new security model.

NOT append-only: ``applied_at`` / ``applied_state`` are UPDATEd on a
``propose`` row when the adjudicator commits/rejects it, so the
forbid_mutation trigger (used by activity_log/credit_ledger) is
deliberately absent here; RLS WITH CHECK still pins every row to its org.

Also adds ``event_quota_per_min`` / ``event_quota_per_day`` to
``executors`` (ADR-0036 §Agent registry & quotas + the c19b5489
anti-runaway gate). Default 0 = unlimited (same convention as
autonomous_daily_credit_cap): a positive cap is opt-in per executor, so
nothing is silently throttled.

Revision ID: 0049
Revises: 0048
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.create_table(
        "event_outbox",
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
        # The acting subject (identity / user id). Not an FK: the row is an
        # immutable audit event that must survive the actor's deletion.
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        # Target node descriptor (nullable: e.g. a snapshot event is graph-wide).
        sa.Column("node_kind", sa.Text(), nullable=True),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=True),
        # propose -> commit/reject audit chain (self-reference).
        sa.Column(
            "parent_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("event_outbox.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "payload_schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        # Producer-supplied dedupe key; the bus dedupes inside a 24h window
        # at the service layer (a windowed dedupe is not a DB unique).
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column(
            "ts", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        # Set when the adjudicator decides a propose (NULL until then).
        sa.Column("applied_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("applied_state", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "actor_kind IN ('human','agent','system')", name="ck_event_outbox_actor_kind"
        ),
        sa.CheckConstraint(
            "kind IN ('read','propose','commit','reject','snapshot')",
            name="ck_event_outbox_kind",
        ),
        sa.CheckConstraint(
            "applied_state IS NULL OR applied_state IN ('committed','rejected','merged')",
            name="ck_event_outbox_applied_state",
        ),
    )
    # Per-tenant totally-ordered stream (audit panel, replay-from-cursor).
    op.create_index("ix_event_outbox_org_ts", "event_outbox", ["org_id", sa.text("ts DESC")])
    # Per-node history (the (org, node) strict ordering of ADR-0036).
    op.create_index(
        "ix_event_outbox_org_node_ts",
        "event_outbox",
        ["org_id", "node_id", sa.text("ts DESC")],
    )
    # 24h idempotency lookup: only rows that carry a key.
    op.create_index(
        "ix_event_outbox_org_idem",
        "event_outbox",
        ["org_id", "idempotency_key", sa.text("ts DESC")],
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.execute("ALTER TABLE event_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE event_outbox FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_event_outbox ON event_outbox USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE event_outbox TO mycelium_app")

    # Deferred NOTIFY at COMMIT: the row is visible to subscribers that
    # LISTEN flow.event and pull it by id. Only the id travels (NOTIFY's
    # 8 KB payload cap). AFTER trigger returning NULL is correct.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_event_outbox() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM pg_notify('flow.event', NEW.id::text);
          RETURN NULL;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_event_outbox_notify
          AFTER INSERT ON event_outbox
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION notify_event_outbox();
        """
    )

    # Per-executor event quotas (ADR-0036 + c19b5489). 0 = unlimited.
    op.add_column(
        "executors",
        sa.Column("event_quota_per_min", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "executors",
        sa.Column("event_quota_per_day", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("executors", "event_quota_per_day")
    op.drop_column("executors", "event_quota_per_min")
    op.execute("DROP TRIGGER IF EXISTS trg_event_outbox_notify ON event_outbox")
    op.execute("DROP FUNCTION IF EXISTS notify_event_outbox()")
    op.execute("DROP POLICY IF EXISTS p_event_outbox ON event_outbox")
    op.drop_index("ix_event_outbox_org_idem", table_name="event_outbox")
    op.drop_index("ix_event_outbox_org_node_ts", table_name="event_outbox")
    op.drop_index("ix_event_outbox_org_ts", table_name="event_outbox")
    op.drop_table("event_outbox")
