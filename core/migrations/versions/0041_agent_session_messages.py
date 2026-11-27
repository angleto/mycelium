"""Messages a person sends to their agent sessions (docs/adr/0079).

``agent_session_messages``  one row per message: the session it is for, the
                     person who wrote it, the text, and its delivery state.
                     Workspace RLS. Deleted with its session.

``agent_sessions.accepts_messages``  said by the host when it opens the
                     session: a relay on its machine delivers messages to it.
                     Every existing session reads false, which is true: none
                     of them runs a relay.

``authenticate_agent_token``  writes ``last_used_at`` only when it is a minute
                     old. Every authenticated call made with an agent token
                     wrote it, and a machine's relays poll every five seconds
                     with one shared CLI token: ten sessions made two updates a
                     second on one row, for a timestamp nothing reads more
                     finely than a minute. The body is spelled out whole
                     because ``CREATE OR REPLACE FUNCTION`` takes a body, not a
                     diff; it is 0012's with one condition added, and its
                     signature is unchanged, so the function keeps its ACL.

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"

# 0012's body with the minute condition on the touch.
_FN_THROTTLED = """
CREATE OR REPLACE FUNCTION public.authenticate_agent_token(
    p_hash bytea,
    OUT out_token_id uuid,
    OUT out_user_id uuid,
    OUT out_org_id uuid,
    OUT out_scope text,
    OUT out_assistant_id uuid,
    OUT out_assistant_scope jsonb,
    OUT out_assistant_active boolean,
    OUT out_workspace_binding text
) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $fn$
    DECLARE
      v_id uuid;
      v_user uuid;
      v_org uuid;
      v_scope text;
      v_expires timestamptz;
      v_revoked timestamptz;
      v_assistant_id uuid;
      v_assistant_scope jsonb;
      v_assistant_active boolean;
      v_user_active boolean;
      v_binding text;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT t.id, t.user_id, t.org_id, t.scope, t.expires_at, t.revoked_at,
             t.assistant_id, a.scope, a.is_active, u.is_active,
             t.workspace_binding::text
        INTO v_id, v_user, v_org, v_scope, v_expires, v_revoked,
             v_assistant_id, v_assistant_scope, v_assistant_active, v_user_active,
             v_binding
        FROM agent_tokens t
        LEFT JOIN ai_assistants a ON a.id = t.assistant_id
        LEFT JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = p_hash;

      IF v_id IS NULL
         OR v_revoked IS NOT NULL
         OR (v_expires IS NOT NULL AND v_expires <= now())
         OR v_user_active IS DISTINCT FROM true
         OR (v_assistant_id IS NOT NULL AND v_assistant_active IS DISTINCT FROM true) THEN
        PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
        PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
        RETURN;
      END IF;

      PERFORM set_config('app.current_org', v_org::text, true);
      PERFORM set_config('app.current_user', v_user::text, true);
      UPDATE agent_tokens SET last_used_at = now(), updated_at = now()
        WHERE id = v_id
          AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute');
      PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
      PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);

      out_token_id := v_id;
      out_user_id := v_user;
      out_org_id := v_org;
      out_scope := v_scope;
      out_assistant_id := v_assistant_id;
      out_assistant_scope := v_assistant_scope;
      out_assistant_active := v_assistant_active;
      -- Never NULL: the column is NOT NULL with a default, and a
      -- credential whose tenancy could not be read must not authenticate
      -- as though it were unconfined.
      out_workspace_binding := coalesce(v_binding, 'workspace');
      RETURN NEXT;
    END
    $fn$;
"""

# 0012's body, byte for byte apart from OR REPLACE, for the downgrade.
_FN_PREVIOUS = """
CREATE OR REPLACE FUNCTION public.authenticate_agent_token(
    p_hash bytea,
    OUT out_token_id uuid,
    OUT out_user_id uuid,
    OUT out_org_id uuid,
    OUT out_scope text,
    OUT out_assistant_id uuid,
    OUT out_assistant_scope jsonb,
    OUT out_assistant_active boolean,
    OUT out_workspace_binding text
) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $fn$
    DECLARE
      v_id uuid;
      v_user uuid;
      v_org uuid;
      v_scope text;
      v_expires timestamptz;
      v_revoked timestamptz;
      v_assistant_id uuid;
      v_assistant_scope jsonb;
      v_assistant_active boolean;
      v_user_active boolean;
      v_binding text;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT t.id, t.user_id, t.org_id, t.scope, t.expires_at, t.revoked_at,
             t.assistant_id, a.scope, a.is_active, u.is_active,
             t.workspace_binding::text
        INTO v_id, v_user, v_org, v_scope, v_expires, v_revoked,
             v_assistant_id, v_assistant_scope, v_assistant_active, v_user_active,
             v_binding
        FROM agent_tokens t
        LEFT JOIN ai_assistants a ON a.id = t.assistant_id
        LEFT JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = p_hash;

      IF v_id IS NULL
         OR v_revoked IS NOT NULL
         OR (v_expires IS NOT NULL AND v_expires <= now())
         OR v_user_active IS DISTINCT FROM true
         OR (v_assistant_id IS NOT NULL AND v_assistant_active IS DISTINCT FROM true) THEN
        PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
        PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
        RETURN;
      END IF;

      PERFORM set_config('app.current_org', v_org::text, true);
      PERFORM set_config('app.current_user', v_user::text, true);
      UPDATE agent_tokens SET last_used_at = now(), updated_at = now()
        WHERE id = v_id;
      PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
      PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);

      out_token_id := v_id;
      out_user_id := v_user;
      out_org_id := v_org;
      out_scope := v_scope;
      out_assistant_id := v_assistant_id;
      out_assistant_scope := v_assistant_scope;
      out_assistant_active := v_assistant_active;
      -- Never NULL: the column is NOT NULL with a default, and a
      -- credential whose tenancy could not be read must not authenticate
      -- as though it were unconfined.
      out_workspace_binding := coalesce(v_binding, 'workspace');
      RETURN NEXT;
    END
    $fn$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.add_column(
        "agent_sessions",
        sa.Column("accepts_messages", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "agent_session_messages",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("author_user_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("state_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure", sa.String(300), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_agent_session_messages"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_agent_session_messages_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["agent_sessions.id"],
            name="fk_agent_session_messages_session_id_agent_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name="fk_agent_session_messages_author_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'claimed', 'delivered', 'failed', 'not_delivered')",
            name="ck_agent_session_messages_state",
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 8000", name="ck_agent_session_messages_body"
        ),
    )
    # What every read takes: a session's messages, oldest first for the
    # claim, newest first for the page.
    op.create_index(
        "ix_agent_session_messages_session",
        "agent_session_messages",
        ["session_id", "created_at"],
    )
    op.execute("ALTER TABLE agent_session_messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_session_messages FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_agent_session_messages ON agent_session_messages "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    create_owner_session_policy(op.get_bind(), "agent_session_messages")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE agent_session_messages TO mycelium_app"
    )
    op.execute(_FN_THROTTLED)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute(_FN_PREVIOUS)
    op.execute("REVOKE ALL ON TABLE agent_session_messages FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS owner_session_all ON agent_session_messages")
    op.execute("DROP POLICY IF EXISTS p_agent_session_messages ON agent_session_messages")
    op.drop_table("agent_session_messages")
    op.drop_column("agent_sessions", "accepts_messages")
