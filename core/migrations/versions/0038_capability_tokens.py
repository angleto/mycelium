"""Capability tokens: ephemeral, single-use, resource-scoped bearer creds.

A capability token authorizes exactly one ``action`` on one resource
(``resource_kind`` / ``resource_id``), expires on a short TTL, and is
consumed on first successful use. It lets the MCP hand an agent a
narrowly-scoped credential for the token-free part-body stream instead
of the operator's long-lived PAT.

``authenticate_capability_token`` mirrors ``authenticate_agent_token``
(baseline): a SECURITY DEFINER lookup that crosses the tenant boundary
at verify time (no GUC selected yet), validating expiry + consumption,
but WITHOUT consuming -- the caller stamps ``consumed_at`` only after the
guarded write succeeds, so a retried 409 does not burn the token.

RLS is ENABLE, not FORCE (same as ``agent_tokens``): the verify function
runs as the table owner and must read a row with no tenant GUC set;
FORCE would subject the owner to the org predicate and the lookup would
find nothing. ``mycelium_app`` (the app role) is NOT the owner, so it stays
fully RLS-confined for the in-tenant mint / consume writes.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_PRED = "org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid"

# SECURITY DEFINER verifier. Shaped like ``authenticate_agent_token``:
# save the caller's GUCs, look the row up by hash, validate, restore the
# GUCs, and return the principal + resource constraint. Read-only (no
# consume here). Owner-run, so the ENABLE-RLS policy does not apply to it.
_AUTH_FN = """
CREATE FUNCTION public.authenticate_capability_token(
    p_hash bytea,
    OUT out_token_id uuid,
    OUT out_user_id uuid,
    OUT out_org_id uuid,
    OUT out_action text,
    OUT out_resource_kind text,
    OUT out_resource_id uuid
) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_id uuid;
      v_user uuid;
      v_org uuid;
      v_action text;
      v_kind text;
      v_resource uuid;
      v_expires timestamptz;
      v_consumed timestamptz;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT t.id, t.user_id, t.org_id, t.action, t.resource_kind,
             t.resource_id, t.expires_at, t.consumed_at
        INTO v_id, v_user, v_org, v_action, v_kind,
             v_resource, v_expires, v_consumed
        FROM capability_tokens t
        WHERE t.token_hash = p_hash;

      PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
      PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);

      IF v_id IS NULL
         OR v_consumed IS NOT NULL
         OR v_expires <= now() THEN
        RETURN;
      END IF;

      out_token_id := v_id;
      out_user_id := v_user;
      out_org_id := v_org;
      out_action := v_action;
      out_resource_kind := v_kind;
      out_resource_id := v_resource;
      RETURN NEXT;
    END
    $$;
"""


def upgrade() -> None:
    op.create_table(
        "capability_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("prefix", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("token_hash", name="uq_capability_tokens_token_hash"),
    )
    op.create_index("ix_capability_tokens_org_id", "capability_tokens", ["org_id"])
    op.create_index("ix_capability_tokens_resource_id", "capability_tokens", ["resource_id"])

    # ENABLE (not FORCE): see module docstring -- the verify function must
    # read a row as the owner with no tenant GUC.
    op.execute("ALTER TABLE capability_tokens ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY p_capability_tokens ON capability_tokens "
        f"USING ({_ORG_PRED}) WITH CHECK ({_ORG_PRED})"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE capability_tokens TO mycelium_app")

    op.execute(_AUTH_FN)
    op.execute("REVOKE ALL ON FUNCTION public.authenticate_capability_token(bytea) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.authenticate_capability_token(bytea) TO mycelium_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.authenticate_capability_token(bytea)")
    op.execute("DROP POLICY IF EXISTS p_capability_tokens ON capability_tokens")
    op.drop_table("capability_tokens")
