"""An agent token stops working when its USER is deactivated.

``authenticate_agent_token`` already refuses a token bound to an
``is_active=false`` ai_assistant, but it never read ``users.is_active``
for the token's own ``user_id``. The API path refuses a deactivated user
on every single request (``deps.current_user``) and at login
(``services.auth.login``); MCP's ``_tenant`` only checks membership. So
deactivating a user locked them out of the SPA and the API while their
agent token kept working through MCP indefinitely -- the one credential
that survives the lock, and the one that acts unattended.

The check belongs HERE and not in the Python caller for two reasons: it
is where the sibling assistant check already lives (one rule, one
place), and the function bumps ``last_used_at`` before returning, so a
refusal decided afterwards would still have recorded the token as used.

DDL only -- no tenant rows are read or written, so this needs no
``owner_sees_all_tenants`` bracket beyond the one ``env.py`` already
wraps every run in.

``CREATE OR REPLACE`` and not ``DROP``+``CREATE``: the OUT parameter
list is the function's return type and is reproduced byte-identically
below, which keeps the OID and therefore the ``REVOKE ... FROM PUBLIC``
/ ``GRANT ALL ... TO mycelium_app`` pair the baseline installs. A DROP
would silently discard both, and the failure would only surface later as
``permission denied for function authenticate_agent_token``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The body as the baseline ships it, minus the user check. Kept verbatim
# so ``downgrade`` restores exactly what was there rather than an
# approximation of it.
_WITHOUT_USER_CHECK = """
CREATE OR REPLACE FUNCTION public.authenticate_agent_token(
    p_hash bytea,
    OUT out_token_id uuid,
    OUT out_user_id uuid,
    OUT out_org_id uuid,
    OUT out_scope text,
    OUT out_assistant_id uuid,
    OUT out_assistant_scope jsonb,
    OUT out_assistant_active boolean
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
    $fn$;
"""


# Same body with the user gate. Three additions, marked below: the
# declaration, the join, and the reject clause.
_WITH_USER_CHECK = """
CREATE OR REPLACE FUNCTION public.authenticate_agent_token(
    p_hash bytea,
    OUT out_token_id uuid,
    OUT out_user_id uuid,
    OUT out_org_id uuid,
    OUT out_scope text,
    OUT out_assistant_id uuid,
    OUT out_assistant_scope jsonb,
    OUT out_assistant_active boolean
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
      -- (1) the token owner's account state.
      v_user_active boolean;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      -- (2) LEFT JOIN and not INNER: ``agent_tokens.user_id`` is NOT NULL
      -- with an FK, so the row is always there in practice, and a LEFT
      -- JOIN makes a missing one land on NULL -> refused by (3) rather
      -- than quietly collapsing the whole SELECT to no row.
      SELECT t.id, t.user_id, t.org_id, t.scope, t.expires_at, t.revoked_at,
             t.assistant_id, a.scope, a.is_active, u.is_active
        INTO v_id, v_user, v_org, v_scope, v_expires, v_revoked,
             v_assistant_id, v_assistant_scope, v_assistant_active, v_user_active
        FROM agent_tokens t
        LEFT JOIN ai_assistants a ON a.id = t.assistant_id
        LEFT JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = p_hash;

      -- (3) ``IS DISTINCT FROM true``, mirroring the assistant clause:
      -- NULL is refused, so an unreadable user fails closed. Unlike the
      -- assistant clause this is unconditional -- every token has a user,
      -- only some have an assistant.
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
      RETURN NEXT;
    END
    $fn$;
"""


def upgrade() -> None:
    op.execute(_WITH_USER_CHECK)


def downgrade() -> None:
    op.execute(_WITHOUT_USER_CHECK)
