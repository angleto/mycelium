"""Drop the dead ``oauth_lookup_assistant`` SECURITY DEFINER function.

Added in migration 0063 to bypass RLS during the OAuth shim's
``/authorize`` step (the request lacks a tenant context). In v1.2.34
we decided to skip the assistant existence check at /authorize
entirely (the real auth gate is /token, where PKCE + client_secret
match against ``authenticate_agent_token``), so the function has
been unused since. Drop it to keep the migration history honest.

Revision: 0066
Down revision: 0065
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0066"
down_revision: str | None = "0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = ("DROP FUNCTION IF EXISTS oauth_lookup_assistant(uuid)",)


DOWNGRADE: tuple[str, ...] = (
    # Re-create the function so a downgrade returns a usable state.
    # Body kept in sync with 0063 verbatim.
    """
    CREATE OR REPLACE FUNCTION oauth_lookup_assistant(in_id uuid)
    RETURNS TABLE (out_active boolean) AS $$
    BEGIN
        RETURN QUERY
            SELECT is_active
            FROM ai_assistants
            WHERE id = in_id;
    END;
    $$ LANGUAGE plpgsql SECURITY DEFINER STABLE
    """,
    "REVOKE ALL ON FUNCTION oauth_lookup_assistant(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION oauth_lookup_assistant(uuid) TO flow_app",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
