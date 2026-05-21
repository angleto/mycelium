"""SECURITY DEFINER function ``oauth_lookup_assistant``: bypass RLS
for the MCP OAuth shim's ``/authorize`` endpoint.

The shim's only job here is to answer "is this client_id (= AI
assistant UUID) a real and active assistant?" before minting an
authorization code. The request hits the backend BEFORE the user
has picked a workspace, so ``app.current_org`` is unset and the RLS
policy on ``ai_assistants`` (``USING (org_id = app.current_org)``)
filters every row out — the SELECT returns nothing and the shim
replies ``invalid_client / unknown or revoked assistant`` even for
a valid live assistant.

Same architectural pattern as ``authenticate_agent_token`` in
migration 0059: a SECURITY DEFINER function that runs as the table
owner (which is not subject to FORCE ROW LEVEL SECURITY for the
SELECT). The function saves + restores the caller's GUCs around its
work so we never leak the bypass to the rest of the session.

Revision: 0063
Down revision: 0062
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
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


DOWNGRADE: tuple[str, ...] = ("DROP FUNCTION IF EXISTS oauth_lookup_assistant(uuid)",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
