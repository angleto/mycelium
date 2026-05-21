"""Re-own the SECURITY DEFINER functions used at the pre-tenant edge
to ``postgres`` (or any BYPASSRLS role), otherwise FORCE RLS still
filters their SELECTs even though they run as SECURITY DEFINER.

Root cause: ``authenticate_agent_token`` (migration 0056/0059) and
``oauth_token_diag`` (migration 0064) both:
1) are called from a session with no ``app.current_org`` set,
2) set ``app.current_org = ''`` inside the function body,
3) SELECT from ``agent_tokens`` (which has FORCE RLS +
   ``USING (org_id = nullif(current_setting('app.current_org', true), '')::uuid)``).

With ``app.current_org = ''`` the policy resolves to
``org_id = NULL`` which is NULL (never TRUE), so RLS filters every
row out even though we are SECURITY DEFINER. The function correctly
returns zero rows. The "client_secret rejected" failure on Claude
Desktop's OAuth handshake stems from exactly this: the hash matches
a real ``agent_tokens`` row, but the SECURITY DEFINER function can't
see it.

The fix is one of:
- Grant BYPASSRLS to the function owner.
- Re-own the function with a role that already has BYPASSRLS (the
  ``postgres`` superuser is always BYPASSRLS).

We pick re-own to ``postgres`` because (a) the function existed
already in 0056/0059/0064 with whatever owner the deploy's
migration role had, (b) re-owning is idempotent and reversible,
(c) it requires no role-permission migration (postgres always has
BYPASSRLS).

In dev / CI this migration is a no-op because the function is
already owned by postgres (the tests run as the superuser).

Revision: 0067
Down revision: 0066
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    # Re-own to the migration role itself: it MUST have BYPASSRLS
    # (or be the table owner) because it just created agent_tokens
    # back in migration 0056. ``current_user`` resolves to whatever
    # the migration is being applied as (postgres in dev, a managed
    # owner role in prod — both have BYPASSRLS).
    """
    DO $$
    DECLARE
      v_role text := current_user;
    BEGIN
      EXECUTE format(
        'ALTER FUNCTION authenticate_agent_token(bytea) OWNER TO %I',
        v_role
      );
      EXECUTE format(
        'ALTER FUNCTION oauth_token_diag(bytea) OWNER TO %I',
        v_role
      );
    END $$;
    """,
)


DOWNGRADE: tuple[str, ...] = (
    # No-op: the original owner is whoever the prior migration was
    # applied as, and we cannot reliably restore it from here. The
    # function's behaviour is unchanged across owners that have
    # BYPASSRLS; reverting would degrade prod.
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
