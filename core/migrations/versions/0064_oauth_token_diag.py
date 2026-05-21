"""SECURITY DEFINER diagnostic helper ``oauth_token_diag``: same
hash lookup as ``authenticate_agent_token`` (migration 0059) but
returns ALL the flags instead of filtering, so the OAuth shim can
log WHY authenticate rejected a bearer ("no row" vs "revoked" vs
"expired" vs "assistant inactive").

Used only on the failure path of /api/oauth/token — never on the
happy path. Not exposed via any HTTP route directly.

Revision: 0064
Down revision: 0063
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0064"
down_revision: str | None = "0063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION oauth_token_diag(p_hash bytea)
    RETURNS TABLE (
        out_exists boolean,
        out_revoked_at timestamptz,
        out_expires_at timestamptz,
        out_assistant_id uuid,
        out_assistant_active boolean
    )
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
    DECLARE
        v_prev_org text := current_setting('app.current_org', true);
        v_prev_user text := current_setting('app.current_user', true);
    BEGIN
        PERFORM set_config('app.current_org', '', true);
        PERFORM set_config('app.current_user', '', true);
        RETURN QUERY
            SELECT
                TRUE,
                t.revoked_at,
                t.expires_at,
                t.assistant_id,
                a.is_active
            FROM agent_tokens t
            LEFT JOIN ai_assistants a ON a.id = t.assistant_id
            WHERE t.token_hash = p_hash;
        IF NOT FOUND THEN
            -- Token hash matches no row.
            RETURN QUERY SELECT FALSE, NULL::timestamptz, NULL::timestamptz,
                                NULL::uuid, NULL::boolean;
        END IF;
        PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
        PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
    END;
    $fn$
    """,
    "REVOKE ALL ON FUNCTION oauth_token_diag(bytea) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION oauth_token_diag(bytea) TO flow_app",
)


DOWNGRADE: tuple[str, ...] = ("DROP FUNCTION IF EXISTS oauth_token_diag(bytea)",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
