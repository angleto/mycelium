"""Public OAuth clients and the consent they wait on (ADR-0069).

Until now the only way to connect an MCP host through OAuth was to create an
AI assistant in the settings first and paste its UUID and secret into the
host. Hosts that implement the MCP authorization specification as written
(Claude, ChatGPT, Cursor, VS Code, ...) do not ask for a secret: they register
themselves (RFC 7591), send the person to an authorization page, and redeem a
code with PKCE. Two tables make that possible.

``oauth_clients``  a public client: a name and its redirect URIs, nothing
                   secret. Written by Dynamic Client Registration (no org, no
                   author, unauthenticated) or by a person in the settings
                   (org and author set). A CHECK pairs the kind with the two
                   columns so neither shape can carry the other's fields.

``ai_assistant_oauth_grants``  for an assistant created by a consent: the
                   client, its redirect, the approver and the moment. Lives
                   as long as the assistant (workspace RLS).

``oauth_authorization_requests``  one ``/authorize`` call waiting for a
                   person, then the approval and the code, then the
                   credential the exchange produced. Its ``client_id`` is
                   not a foreign key: a Client ID Metadata Document client
                   is an https URL that no table holds, so the client's name
                   and kind are snapshotted at /authorize instead. The device grant's
                   table with the OAuth parameters added; see the model for
                   why it is not ``oauth_codes``.

Row-level security, and why only on the first table. ``oauth_clients`` holds
rows that belong to a workspace (the manual ones), so a person's session must
see its own and no other workspace's. Two policies:

- ``p_oauth_clients``: the ordinary tenant policy. A session with a workspace
  sees and writes the rows of that workspace; a dynamic row, having none, is
  invisible to it.
- ``p_oauth_clients_system``: the system actor with NO workspace -- the
  admin session the pre-tenant OAuth endpoints and the sweep run in -- sees
  and writes every row. A client id arrives at ``/authorize`` before anybody
  has logged in, so the lookup cannot be tenant-scoped, and a dynamic
  registration has no tenant to be written under.

``oauth_authorization_requests`` has no tenant at the moment it is written,
like ``device_authorizations`` and ``refresh_tokens``, so there is no policy to
write; every read of it is by the digest of an unguessable value.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from mycelium_core.migration_rls import create_owner_session_policy

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"
_SYSTEM_NO_ORG = (
    "current_setting('app.current_actor_kind'::text, true) = 'system'::text "
    "AND NULLIF(current_setting('app.current_org'::text, true), ''::text) IS NULL"
)


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_name", sa.String(length=100), nullable=False),
        sa.Column("redirect_uris", JSONB(), nullable=False),
        sa.Column("registration", sa.String(length=16), nullable=False),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("origin_ip", sa.String(length=45), nullable=True),
        sa.Column("first_grant_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_clients"),
        sa.UniqueConstraint("client_id", name="uq_oauth_clients_client_id"),
        sa.CheckConstraint(
            "registration IN ('dynamic', 'manual')",
            name="ck_oauth_clients_registration",
        ),
        # A manual client has a workspace and an author; a dynamic one has
        # neither. Enforced here rather than trusted to the service, because
        # the RLS policies above read ``org_id`` to decide who sees a row, and
        # a dynamic row that carried one would appear in that workspace.
        sa.CheckConstraint(
            "(registration = 'manual') = (org_id IS NOT NULL AND created_by_id IS NOT NULL)"
            " AND (org_id IS NULL) = (created_by_id IS NULL)",
            name="ck_oauth_clients_manual_has_owner",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_oauth_clients_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name="fk_oauth_clients_created_by_id_users",
            ondelete="CASCADE",
        ),
    )
    # The two questions asked of this table in bulk: "how many unfinished
    # registrations from this address lately" (the bound on the open
    # endpoint) and "which unfinished registrations are old" (the sweep).
    op.create_index(
        "ix_oauth_clients_unused_created_at",
        "oauth_clients",
        ["created_at"],
        postgresql_where=sa.text("registration = 'dynamic' AND first_grant_at IS NULL"),
    )
    op.create_index("ix_oauth_clients_org_id", "oauth_clients", ["org_id"])

    op.execute("ALTER TABLE oauth_clients ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE oauth_clients FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_oauth_clients ON oauth_clients "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute(
        "CREATE POLICY p_oauth_clients_system ON oauth_clients "
        f"USING ({_SYSTEM_NO_ORG}) WITH CHECK ({_SYSTEM_NO_ORG})"
    )
    create_owner_session_policy(op.get_bind(), "oauth_clients")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE oauth_clients TO mycelium_app")

    op.create_table(
        "oauth_authorization_requests",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("request_id_hash", sa.String(length=64), nullable=False),
        # Not a foreign key: a client id is whatever a strategy resolves --
        # a registered row, but also an https URL (a Client ID Metadata
        # Document) that exists in no table. The name and kind shown on the
        # consent page are snapshotted from the resolution at /authorize, so
        # neither the page nor the exchange has to resolve the client again.
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_name", sa.String(length=100), nullable=False),
        sa.Column("client_registration", sa.String(length=32), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("resource", sa.Text(), nullable=True),
        sa.Column("requested_scope", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("approved_org_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("granted_scope", JSONB(), nullable=True),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assistant_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("origin_ip", sa.String(length=45), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_authorization_requests"),
        sa.UniqueConstraint(
            "request_id_hash", name="uq_oauth_authorization_requests_request_id_hash"
        ),
        sa.UniqueConstraint("code_hash", name="uq_oauth_authorization_requests_code_hash"),
        sa.CheckConstraint(
            "length(client_id) <= 2048", name="ck_oauth_authorization_requests_client_id_length"
        ),
        sa.ForeignKeyConstraint(
            ["answered_by_id"],
            ["users.id"],
            name="fk_oauth_authorization_requests_answered_by_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approved_org_id"],
            ["organizations.id"],
            name="fk_oauth_authorization_requests_approved_org_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["ai_assistants.id"],
            name="fk_oauth_authorization_requests_assistant_id_ai_assistants",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_oauth_authorization_requests_client_id",
        "oauth_authorization_requests",
        ["client_id"],
    )
    op.create_index(
        "ix_oauth_authorization_requests_expires_at",
        "oauth_authorization_requests",
        ["expires_at"],
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE oauth_authorization_requests TO mycelium_app"
    )

    # Provenance of an assistant created by a consent: which client holds its
    # bearer, where that client's codes go, who approved it and when. The
    # authorization request that carries the same facts is swept a week after
    # it expires; this row lives as long as the assistant, so the settings
    # can always answer "who holds this token" and the person can revoke it.
    # Tenant data, under the ordinary workspace policy.
    op.create_table(
        "ai_assistant_oauth_grants",
        sa.Column("assistant_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_registration", sa.String(length=32), nullable=False),
        sa.Column("client_name", sa.String(length=100), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("approved_by_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("assistant_id", name="pk_ai_assistant_oauth_grants"),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["ai_assistants.id"],
            name="fk_ai_assistant_oauth_grants_assistant_id_ai_assistants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_ai_assistant_oauth_grants_org_id_organizations",
            ondelete="CASCADE",
        ),
        # SET NULL: the approver leaving the workspace must not erase the
        # record that a client holds a token they approved.
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name="fk_ai_assistant_oauth_grants_approved_by_id_users",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_ai_assistant_oauth_grants_org_id", "ai_assistant_oauth_grants", ["org_id"])
    op.execute("ALTER TABLE ai_assistant_oauth_grants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_assistant_oauth_grants FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY p_ai_assistant_oauth_grants ON ai_assistant_oauth_grants "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    create_owner_session_policy(op.get_bind(), "ai_assistant_oauth_grants")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE ai_assistant_oauth_grants TO mycelium_app"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON TABLE ai_assistant_oauth_grants FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS owner_session_all ON ai_assistant_oauth_grants")
    op.execute("DROP POLICY IF EXISTS p_ai_assistant_oauth_grants ON ai_assistant_oauth_grants")
    op.drop_index("ix_ai_assistant_oauth_grants_org_id", table_name="ai_assistant_oauth_grants")
    op.drop_table("ai_assistant_oauth_grants")

    op.execute("REVOKE ALL ON TABLE oauth_authorization_requests FROM mycelium_app")
    op.drop_index(
        "ix_oauth_authorization_requests_expires_at", table_name="oauth_authorization_requests"
    )
    op.drop_index(
        "ix_oauth_authorization_requests_client_id", table_name="oauth_authorization_requests"
    )
    op.drop_table("oauth_authorization_requests")

    op.execute("REVOKE ALL ON TABLE oauth_clients FROM mycelium_app")
    op.execute("DROP POLICY IF EXISTS owner_session_all ON oauth_clients")
    op.execute("DROP POLICY IF EXISTS p_oauth_clients_system ON oauth_clients")
    op.execute("DROP POLICY IF EXISTS p_oauth_clients ON oauth_clients")
    op.drop_index("ix_oauth_clients_org_id", table_name="oauth_clients")
    op.drop_index("ix_oauth_clients_unused_created_at", table_name="oauth_clients")
    op.drop_table("oauth_clients")
