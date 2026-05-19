"""Make provision_organization portable: no BYPASSRLS required.

Until 0049 the SECURITY DEFINER ``provision_organization`` relied on
its owner (the ``flow`` role) having ``BYPASSRLS`` so the cross-tenant
INSERT into the FORCE-RLS ``organizations`` (and the cascading INSERT
into ``memberships``) could pass the policy that requires
``id = current_setting('app.current_org')::uuid``. ``BYPASSRLS`` is a
``SUPERUSER``-only attribute on managed Postgres providers (Scaleway
explicitly rejects ``ALTER ROLE ... BYPASSRLS`` with
``ROLE modification to SUPERUSER/privileged role not allowed``), so
the function never worked in production there: signup blew up with
``new row violates row-level security policy for table
"organizations"``.

The portable fix: generate the new org id in PL/pgSQL, set the tenant
GUCs (``app.current_org`` / ``app.current_user``) **transaction-local
inside the function** with ``set_config(..., true)``, then INSERT.
The RLS policies (``id = app.current_org`` on ``organizations``;
``org_id = app.current_org`` on the tenant tables) all pass with the
correct semantics, the function owner does not need ``BYPASSRLS``,
and the policy itself is unchanged (no security weakening).

Revision: 0050
Down revision: 0049
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


_UPGRADE_FN = """
CREATE OR REPLACE FUNCTION provision_organization(
  p_name text, p_user_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_org uuid := gen_random_uuid();
BEGIN
  -- Set the tenant GUCs FIRST so the RLS policies on organizations
  -- (``id = app.current_org``) and on every tenant table
  -- (``org_id = app.current_org``) pass the WITH CHECK of the
  -- INSERTs below without the function owner needing BYPASSRLS
  -- (managed Postgres providers do not allow that attribute).
  -- ``true`` => transaction-local, no leak across pool connections.
  PERFORM set_config('app.current_org', v_org::text, true);
  PERFORM set_config('app.current_user', p_user_id::text, true);
  INSERT INTO organizations (id, name) VALUES (v_org, p_name);
  INSERT INTO memberships (org_id, user_id, role)
    VALUES (v_org, p_user_id, 'owner');
  PERFORM create_default_workflow(v_org);
  PERFORM create_default_calendar(v_org);
  RETURN v_org;
END
$fn$
"""

# 0005's last definition, kept identical for downgrade.
_DOWNGRADE_FN = """
CREATE OR REPLACE FUNCTION provision_organization(
  p_name text, p_user_id uuid
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_org uuid;
BEGIN
  INSERT INTO organizations (name) VALUES (p_name)
    RETURNING id INTO v_org;
  INSERT INTO memberships (org_id, user_id, role)
    VALUES (v_org, p_user_id, 'owner');
  PERFORM create_default_workflow(v_org);
  PERFORM create_default_calendar(v_org);
  RETURN v_org;
END
$fn$
"""


def upgrade() -> None:
    op.execute(_UPGRADE_FN)


def downgrade() -> None:
    op.execute(_DOWNGRADE_FN)
