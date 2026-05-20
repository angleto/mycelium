"""Agent tokens: long-lived bearer credentials for MCP / external automation.

A user (typically the workspace owner) mints one or more ``agent tokens``
to grant a Claude Desktop / MCP client / external automation the ability
to act on the API + MCP surface as themselves, without embedding a JWT
that expires every hour.

Storage discipline (adapted from the bitvision_phoenix pattern):

- The *raw* token (``flow_at_<32 url-safe chars>``) is returned to the
  operator EXACTLY ONCE at create time, never echoed afterward.
- The DB only ever holds ``sha256(raw_bytes)`` plus a short non-secret
  prefix for disambiguation in the UI (e.g. ``flow_at_AbCdEfGh``).
- Verification recomputes the hash from the presented bearer and looks
  the row up by it (O(1) on the unique index), so a DB dump does not
  leak usable tokens and revocation is a single UPDATE.

RLS: standard ``OrgScopedMixin`` (FORCE RLS, policy keys on
``app.current_org``). Listing / minting / revoking happen inside a
``tenant_session`` -- the row lookup is org-scoped like any other tenant
table.

Authentication has to cross the tenant boundary: the MCP server (and any
external automation) presents the bearer with no Flow context yet, so we
need to find the row WITHOUT a ``app.current_org`` GUC. A SECURITY
DEFINER function ``authenticate_agent_token(p_hash bytea)`` does the
lookup, validates expiry / revocation, bumps ``last_used_at``, and
returns the principal (user_id, org_id, scope). It saves the caller's
GUCs and restores them before returning, same idiom as
``provision_organization`` / ``list_user_organizations`` (migrations
0050-0053): no BYPASSRLS needed, the function sets the right tenant
context transaction-local.

Revision: 0056
Down revision: 0055
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ORG = "nullif(current_setting('app.current_org', true), '')::uuid"


UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE agent_tokens (
      id uuid NOT NULL DEFAULT gen_random_uuid(),
      org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
      user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      name varchar(120) NOT NULL,
      prefix varchar(20) NOT NULL,
      token_hash bytea NOT NULL,
      scope varchar(32) NOT NULL DEFAULT 'mcp',
      expires_at timestamptz,
      last_used_at timestamptz,
      revoked_at timestamptz,
      version bigint NOT NULL DEFAULT 1,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_agent_tokens PRIMARY KEY (id),
      CONSTRAINT uq_agent_tokens_token_hash UNIQUE (token_hash),
      CONSTRAINT ck_agent_tokens_name_len CHECK (length(name) BETWEEN 1 AND 120),
      CONSTRAINT ck_agent_tokens_scope_len CHECK (length(scope) BETWEEN 1 AND 32)
    )
    """,
    "CREATE INDEX ix_agent_tokens_org_id ON agent_tokens (org_id)",
    "CREATE INDEX ix_agent_tokens_org_user_active ON agent_tokens (org_id, user_id, revoked_at)",
    "ALTER TABLE agent_tokens ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE agent_tokens FORCE ROW LEVEL SECURITY",
    (
        "CREATE POLICY p_agent_tokens ON agent_tokens "
        f"USING (org_id = {_ORG}) WITH CHECK (org_id = {_ORG})"
    ),
    "GRANT SELECT, INSERT, UPDATE, DELETE ON agent_tokens TO flow_app",
    # SECURITY DEFINER hash lookup. Called from a session with NO
    # tenant context (MCP / external bearer auth: the request has not
    # yet selected a workspace). The function sets app.current_org /
    # app.current_user transaction-local so the SELECT + UPDATE pass
    # the FORCE-RLS policy above, then restores the caller's previous
    # GUCs before returning, same idiom as provision_organization
    # (migration 0052) and the workspace funcs in 0053. No BYPASSRLS.
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
      -- Look the row up under elevated rights (no tenant GUC yet).
      -- We are SECURITY DEFINER so RLS evaluates as the function
      -- owner, BUT FORCE RLS still applies. Clear any GUC for the
      -- duration of the SELECT so the row is visible regardless of
      -- whatever (unrelated) workspace the caller may previously
      -- have been in.
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT t.id, t.user_id, t.org_id, t.scope, t.expires_at, t.revoked_at
        INTO v_id, v_user, v_org, v_scope, v_expires, v_revoked
        FROM agent_tokens t
        WHERE t.token_hash = p_hash;

      IF v_id IS NULL
         OR v_revoked IS NOT NULL
         OR (v_expires IS NOT NULL AND v_expires <= now()) THEN
        PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
        PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
        RETURN;
      END IF;

      -- Bump last_used_at. Set the tenant GUC so the UPDATE's
      -- WITH CHECK clause passes (the row's org_id = current_org).
      PERFORM set_config('app.current_org', v_org::text, true);
      PERFORM set_config('app.current_user', v_user::text, true);
      UPDATE agent_tokens SET last_used_at = now(), updated_at = now()
        WHERE id = v_id;

      -- Restore the caller's GUCs: the caller (e.g. MCP _tenant)
      -- will set them explicitly when it opens its own
      -- tenant_session for the real request work.
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
)


DOWNGRADE: tuple[str, ...] = (
    "DROP FUNCTION IF EXISTS authenticate_agent_token(bytea)",
    "DROP TABLE IF EXISTS agent_tokens CASCADE",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
