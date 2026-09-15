"""A capability token can be revoked, and it remembers who minted it.

``capability_tokens`` had ``expires_at`` and ``consumed_at`` and no way
to say "this grant is withdrawn". A minted capability could only be
waited out. That is tolerable at the five-minute default and not at the
one-hour ceiling, and it is no answer at all for one that leaked: the raw
value is handed back exactly once, and whoever minted it could not take
it away again (task 1428a184).

TWO halves, and the column alone would be decoration. Authentication does
not read the table from the caller's session: it goes through the
SECURITY DEFINER ``authenticate_capability_token``, which crosses the
tenant boundary to do the lookup. A revocation the function does not
check is a revocation that does not revoke, so the function is replaced
in the same migration.

The check joins the two that are already there -- consumed, expired --
and reads the same way: three reasons a row is no longer a credential,
one ``RETURN``. Adding it in Python after the call would have put the
predicate in two places and let the SQL keep answering "valid".

Nullable, no backfill: a NULL means "not revoked", which is true of
every row that exists.

THE SECOND COLUMN is the one that removes a defect rather than fencing
it. ``capability_tokens`` recorded ``user_id`` and nothing about the
CREDENTIAL that minted the grant, so at redemption the system could not
re-evaluate whether that credential may still do the thing. The app-level
scope gate says as much in a comment and returns early for a capability:
"it carries its own action/resource authorization". True of the action
and the resource; false of the AUTHORITY, which was decided once at mint
and frozen -- ambient authority by construction.

``minted_by_agent_token_id`` makes the grant a DELEGATION of a live
authority instead of a snapshot of a dead one: the gate resolves it and
applies that credential's scope to the route, so revoking the agent token
revokes every capability it minted, and narrowing an assistant's scope
narrows them too. NULL means a human's own bearer minted it, which is the
case for every row that exists and needs no check.

ON DELETE SET NULL, not CASCADE: deleting the agent token must not delete
the audit trail of grants it made. A capability whose minter is gone
falls back to the human-minted reading, which is safe because the row is
already expired or consumed by then -- the ceiling is one hour.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The function as it stands plus the revocation clause. Spelled out whole
# rather than patched, because ``CREATE OR REPLACE FUNCTION`` takes a body
# and not a diff -- and because a reader comparing this against the
# baseline can see exactly one line of difference.
_FN = """
CREATE OR REPLACE FUNCTION public.authenticate_capability_token(
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
      v_revoked timestamptz;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT t.id, t.user_id, t.org_id, t.action, t.resource_kind,
             t.resource_id, t.expires_at, t.consumed_at, t.revoked_at
        INTO v_id, v_user, v_org, v_action, v_kind,
             v_resource, v_expires, v_consumed, v_revoked
        FROM capability_tokens t
        WHERE t.token_hash = p_hash;

      PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
      PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);

      IF v_id IS NULL
         OR v_consumed IS NOT NULL
         OR v_revoked IS NOT NULL
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

_FN_WITHOUT_REVOCATION = (
    _FN.replace(
        "             t.resource_id, t.expires_at, t.consumed_at, t.revoked_at",
        "             t.resource_id, t.expires_at, t.consumed_at",
    )
    .replace(
        "             v_resource, v_expires, v_consumed, v_revoked",
        "             v_resource, v_expires, v_consumed",
    )
    .replace(
        "         OR v_revoked IS NOT NULL\n",
        "",
    )
    .replace(
        "      v_revoked timestamptz;\n",
        "",
    )
)


def upgrade() -> None:
    op.add_column(
        "capability_tokens",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "capability_tokens",
        sa.Column(
            "minted_by_agent_token_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_tokens.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.execute(_FN)


def downgrade() -> None:
    # The function goes back first: dropping the column under a function
    # that still selects it would leave every capability authentication
    # raising until the next statement landed.
    op.execute(_FN_WITHOUT_REVOCATION)
    op.drop_column("capability_tokens", "minted_by_agent_token_id")
    op.drop_column("capability_tokens", "revoked_at")
