"""Sessions a person steers (docs/adr/0078).

``agent_sessions``   one row per process of an agent host (a Claude Code
                     session), opened by the host's hooks through the CLI:
                     project, machine, and whether it is waiting for its
                     person, as a claim with a date. Workspace RLS. Retry key
                     ``(opened_by_token_id, agent, host_ref)``, so a resume
                     finds the row it opened. The join key is stored as a
                     hash only.

``agent_workers.session_id``  the session a worker joined, SET NULL: a
                     session row going away leaves the worker, and what it
                     holds, alone.

Nothing is backfilled. A worker open right now joined no session, which is
true.

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.create_table(
        "agent_sessions",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("opened_by_user_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("opened_by_identity_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("opened_by_token_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("host_ref", sa.String(128), nullable=False),
        sa.Column("project", sa.String(128), nullable=True),
        sa.Column("machine", sa.String(128), nullable=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("attention", sa.String(16), nullable=False),
        sa.Column("attention_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_on", sa.String(16), nullable=True),
        sa.Column("join_key_hash", sa.String(64), nullable=False),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(16), nullable=True),
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
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("id", name="pk_agent_sessions"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_agent_sessions_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opened_by_user_id"],
            ["users.id"],
            name="fk_agent_sessions_opened_by_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opened_by_identity_id"],
            ["identities.id"],
            name="fk_agent_sessions_opened_by_identity_id_identities",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("agent IN ('claude_code')", name="ck_agent_sessions_agent"),
        sa.CheckConstraint(
            "attention IN ('working', 'blocked', 'your_turn')",
            name="ck_agent_sessions_attention",
        ),
        sa.CheckConstraint(
            "(attention = 'blocked') = (blocked_on IS NOT NULL)",
            name="ck_agent_sessions_blocked_on",
        ),
        sa.CheckConstraint(
            "blocked_on IS NULL OR blocked_on IN ('permission', 'question')",
            name="ck_agent_sessions_blocked_on_value",
        ),
        sa.CheckConstraint(
            "(closed_at IS NULL) = (closed_reason IS NULL)",
            name="ck_agent_sessions_closed_reason",
        ),
        sa.CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ('ended', 'gone', 'idle')",
            name="ck_agent_sessions_closed_reason_value",
        ),
    )
    # The retry path: one session per (credential, host, host's id).
    op.create_index(
        "uq_agent_sessions_host_ref",
        "agent_sessions",
        ["opened_by_token_id", "agent", "host_ref"],
        unique=True,
    )
    # What the page and the sweep read: the open sessions of a workspace.
    op.create_index(
        "ix_agent_sessions_open",
        "agent_sessions",
        ["org_id", "last_seen_at"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.execute("ALTER TABLE agent_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_agent_sessions ON agent_sessions USING ({_ORG_PRED}) "
        f"WITH CHECK ({_ORG_PRED})"
    )
    create_owner_session_policy(op.get_bind(), "agent_sessions")
    op.execute("GRANT SELECT, INSERT, UPDATE ON TABLE agent_sessions TO mycelium_app")

    op.add_column(
        "agent_workers",
        sa.Column("session_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_workers_session_id_agent_sessions",
        "agent_workers",
        "agent_sessions",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_agent_workers_session",
        "agent_workers",
        ["session_id"],
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.drop_index("ix_agent_workers_session", table_name="agent_workers")
    op.drop_constraint(
        "fk_agent_workers_session_id_agent_sessions", "agent_workers", type_="foreignkey"
    )
    op.drop_column("agent_workers", "session_id")
    op.execute("REVOKE ALL ON TABLE agent_sessions FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS owner_session_all ON agent_sessions")
    op.execute("DROP POLICY IF EXISTS p_agent_sessions ON agent_sessions")
    op.drop_table("agent_sessions")
