"""SdI inbound correlation: resolve the tenant of a push notification by
IdentificativoSdI (docs/adr/0011, FR-9 / F7b).

One accredited channel serves all tenants, so an inbound SdI notification is
correlated to the right org by ``IdentificativoSdI`` -- a cross-org lookup
with no tenant context (like the pre-tenant ``authenticate_agent_token``).
Following the 0068 owner-bypass pattern: drop FORCE on ``invoices`` (RLS stays
ENABLED, so the runtime role ``flow_app`` is still org-scoped; only the table
owner bypasses) and add a SECURITY DEFINER resolver -- owned by the table
owner, so once FORCE is gone it bypasses RLS -- that returns only the org_id.
The status update itself then runs through a normal tenant_session.

Revision: 0074
Down revision: 0073
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RESOLVER = """
CREATE OR REPLACE FUNCTION sdi_resolve_invoice_org(p_identificativo text)
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
  SELECT org_id FROM invoices WHERE identificativo_sdi = p_identificativo LIMIT 1
$fn$
"""


UPGRADE: tuple[str, ...] = (
    # Canonical Postgres: the table owner bypasses RLS once FORCE is gone
    # (the SECURITY DEFINER resolver below relies on it). flow_app stays
    # org-scoped because RLS remains ENABLED. Same posture as 0068.
    "ALTER TABLE invoices NO FORCE ROW LEVEL SECURITY",
    _RESOLVER,
    "GRANT EXECUTE ON FUNCTION sdi_resolve_invoice_org(text) TO flow_app",
)


DOWNGRADE: tuple[str, ...] = (
    "DROP FUNCTION IF EXISTS sdi_resolve_invoice_org(text)",
    "ALTER TABLE invoices FORCE ROW LEVEL SECURITY",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
