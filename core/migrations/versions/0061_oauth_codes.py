"""OAuth authorization-code store for the MCP OAuth 2.1 + PKCE shim.

Ports the schema from bitvision_phoenix
(``backend/src/bvphoenix/db/models/oauth_codes.py``). The shim mints
a code at ``GET /authorize`` and consumes it at ``POST /token``; with
multiple backend replicas an in-memory store would route the two
endpoints to different pods and break the handshake. Persisting in
Postgres is the central design choice.

Codes are single-use, short-lived (default TTL 10 min), and trimmed
to a max length of 64 chars. The PKCE ``code_challenge`` (S256,
43-char base64url) is bound at mint and verified at consume; the
service just returns the metadata it stored.

Revision: 0061
Down revision: 0060
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE oauth_codes (
        code varchar(64) PRIMARY KEY,
        client_id varchar(64) NOT NULL,
        redirect_uri text NOT NULL,
        code_challenge varchar(128) NOT NULL,
        code_challenge_method varchar(16) NOT NULL,
        expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_oauth_codes_expires_at ON oauth_codes (expires_at)",
    # Not RLS-scoped: codes are bound to a client_id (= AI assistant
    # id) which is workspace-scoped indirectly, but the OAuth shim
    # operates without a tenant context (the request comes from the
    # browser BEFORE the user picks a workspace; the code is the
    # carrier of the binding). RLS would block the mint / consume; we
    # gate authorisation by validating client_id + client_secret
    # against the existing agent_token system in the shim itself.
)


DOWNGRADE: tuple[str, ...] = (
    "DROP INDEX IF EXISTS ix_oauth_codes_expires_at",
    "DROP TABLE IF EXISTS oauth_codes",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
