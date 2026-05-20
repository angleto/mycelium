"""ai_assistants: per-user AI assistant identity + scope bound to an agent_token.

Adds a one-to-many parent over ``agent_tokens`` (one active credential
per assistant, rotated by minting + revoking) and a per-assistant scope
list. The token surface is unchanged: clients keep presenting
``flow_at_<...>`` bearers; the SECURITY DEFINER lookup now also returns
the assistant id and its scope so the MCP gate can filter tools.

Pattern mirrored from bitvision_phoenix's ``ai_assistants`` /
``agent_tokens`` split (docs: docs/ai-assistants.md). Per-user assistant
ownership; RLS scopes the row to the workspace exactly like
``agent_tokens``.

Revision: 0059
Down revision: 0058
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"


UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE ai_assistants (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      label varchar(255) NOT NULL,
      provider varchar(64),
      model_id varchar(128),
      notes text,
      scope jsonb NOT NULL DEFAULT '[]'::jsonb,
      is_active boolean NOT NULL DEFAULT true,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_ai_assistants PRIMARY KEY (id),
      CONSTRAINT ck_ai_assistants_label_len CHECK (length(label) BETWEEN 1 AND 255)
    )
    """,
    "CREATE INDEX ix_ai_assistants_org_user ON ai_assistants (org_id, user_id)",
    "ALTER TABLE ai_assistants ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE ai_assistants FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_ai_assistants ON ai_assistants "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ai_assistants TO flow_app",
    # Bind agent_tokens to an assistant. Nullable for back-compat:
    # legacy tokens minted before 0057 keep working with full MCP
    # surface (no scope filter), the UI funnels new mints through the
    # assistant flow.
    "ALTER TABLE agent_tokens ADD COLUMN assistant_id uuid "
    "REFERENCES ai_assistants(id) ON DELETE CASCADE",
    "CREATE INDEX ix_agent_tokens_assistant_id ON agent_tokens (assistant_id)",
    # Replace authenticate_agent_token to also surface
    # (assistant_id, assistant_scope, assistant_active) so the MCP gate
    # can deny a tool that isn't in the assistant's scope without a
    # second round-trip. Same idiom as 0056: SECURITY DEFINER, no
    # BYPASSRLS, transaction-local GUCs, last_used_at bump.
    "DROP FUNCTION IF EXISTS authenticate_agent_token(bytea)",
    """
    CREATE FUNCTION authenticate_agent_token(
      p_hash bytea,
      OUT out_token_id uuid,
      OUT out_user_id uuid,
      OUT out_org_id uuid,
      OUT out_scope text,
      OUT out_assistant_id uuid,
      OUT out_assistant_scope jsonb,
      OUT out_assistant_active boolean
    )
    RETURNS SETOF record
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
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
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT t.id, t.user_id, t.org_id, t.scope, t.expires_at, t.revoked_at,
             t.assistant_id, a.scope, a.is_active
        INTO v_id, v_user, v_org, v_scope, v_expires, v_revoked,
             v_assistant_id, v_assistant_scope, v_assistant_active
        FROM agent_tokens t
        LEFT JOIN ai_assistants a ON a.id = t.assistant_id
        WHERE t.token_hash = p_hash;

      IF v_id IS NULL
         OR v_revoked IS NOT NULL
         OR (v_expires IS NOT NULL AND v_expires <= now())
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
      RETURN NEXT;
    END
    $fn$
    """,
    "REVOKE ALL ON FUNCTION authenticate_agent_token(bytea) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION authenticate_agent_token(bytea) TO flow_app",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP FUNCTION IF EXISTS authenticate_agent_token(bytea)",
    # Restore the pre-0057 4-output signature verbatim from 0056.
    """
    CREATE FUNCTION authenticate_agent_token(
      p_hash bytea,
      OUT out_token_id uuid,
      OUT out_user_id uuid,
      OUT out_org_id uuid,
      OUT out_scope text
    )
    RETURNS SETOF record
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    DECLARE
      v_id uuid;
      v_user uuid;
      v_org uuid;
      v_scope text;
      v_expires timestamptz;
      v_revoked timestamptz;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);
      SELECT t.id, t.user_id, t.org_id, t.scope, t.expires_at, t.revoked_at
        INTO v_id, v_user, v_org, v_scope, v_expires, v_revoked
        FROM agent_tokens t
        WHERE t.token_hash = p_hash;
      IF v_id IS NULL OR v_revoked IS NOT NULL
         OR (v_expires IS NOT NULL AND v_expires <= now()) THEN
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
      RETURN NEXT;
    END
    $fn$
    """,
    "REVOKE ALL ON FUNCTION authenticate_agent_token(bytea) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION authenticate_agent_token(bytea) TO flow_app",
    "DROP INDEX IF EXISTS ix_agent_tokens_assistant_id",
    "ALTER TABLE agent_tokens DROP COLUMN IF EXISTS assistant_id",
    "DROP TABLE IF EXISTS ai_assistants CASCADE",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
