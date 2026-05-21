"""Drop FORCE ROW LEVEL SECURITY on agent_tokens + ai_assistants so
the SECURITY DEFINER pre-tenant lookups can read them.

The SECURITY DEFINER functions ``authenticate_agent_token`` (0056/
0059) and ``oauth_token_diag`` (0064) run BEFORE the request has a
tenant context, so they can't satisfy the RLS policy
``USING (org_id = current_org)``. They set ``app.current_org = ''``
which evaluates the policy to ``org_id = NULL`` → NULL → row hidden.

The Postgres default is: the TABLE OWNER bypasses RLS unless FORCE
is set. With ``FORCE ROW LEVEL SECURITY`` the owner is also subject
to RLS, which is what's blocking the SECURITY DEFINER functions
here. Migration 0067 tried to address this by re-owning the
functions to a BYPASSRLS role, but in managed Postgres the role
applying the migration doesn't always have BYPASSRLS by default.

Dropping FORCE re-enables the canonical Postgres behavior: the
table owner bypasses RLS. The SECURITY DEFINER function, owned by
the same role as the table (both created in 0056), bypasses on
its behalf. RLS itself stays ENABLED and continues to scope every
other role's queries (flow_app, the app runtime); only the table
owner is exempted.

This is the same security posture as Flow's other tenant-scoped
tables (tasks, notes, time_entries, ...) which never had FORCE
RLS to begin with.

Revision: 0068
Down revision: 0067
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0068"
down_revision: str | None = "0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE: tuple[str, ...] = (
    "ALTER TABLE agent_tokens NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE ai_assistants NO FORCE ROW LEVEL SECURITY",
)


DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE ai_assistants FORCE ROW LEVEL SECURITY",
    "ALTER TABLE agent_tokens FORCE ROW LEVEL SECURITY",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
