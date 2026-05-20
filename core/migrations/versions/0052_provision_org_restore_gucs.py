"""provision_organization + list_user_organizations: restore caller GUCs.

The 0050 ``provision_organization`` set ``app.current_org`` and
``app.current_user`` transaction-local with ``set_config(.., true)``
to make RLS pass without BYPASSRLS, but it did NOT restore the
caller's previous GUC values before returning. A nested call (e.g.
``signup`` inside an outer ``tenant_session(outer_org, owner)``)
therefore leaves the GUCs pointing at the freshly-created org/user --
and any subsequent INSERT in the same outer transaction (test fixtures
often do this: create an OG2 user via signup, then attach a Membership
to the outer OG) violates the WITH CHECK clause because the GUC no
longer matches the row's org_id.

Same problem in 0051's ``list_user_organizations``.

Fix: save the previous GUC values at function entry and restore them
before returning. Identical behaviour from the caller's perspective.

Revision: 0052
Down revision: 0051
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


_PROVISION_V2 = """
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
  v_prev_org text := current_setting('app.current_org', true);
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  PERFORM set_config('app.current_org', v_org::text, true);
  PERFORM set_config('app.current_user', p_user_id::text, true);
  INSERT INTO organizations (id, name) VALUES (v_org, p_name);
  INSERT INTO memberships (org_id, user_id, role)
    VALUES (v_org, p_user_id, 'owner');
  PERFORM create_default_workflow(v_org);
  PERFORM create_default_calendar(v_org);
  -- Restore caller's GUCs so a nested call (e.g. signup inside an
  -- outer tenant_session) does not leave app.current_org/_user
  -- pointing at the new org for the rest of the caller's transaction.
  PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
  RETURN v_org;
END
$fn$
"""

_PROVISION_V1 = """
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

_LIST_USER_ORGS_V2 = """
CREATE FUNCTION list_user_organizations(p_user_id uuid)
RETURNS TABLE(org_id uuid, name text, role text, status text)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  PERFORM set_config('app.current_user', p_user_id::text, true);
  RETURN QUERY
    SELECT o.id, o.name::text, m.role::text, o.status::text
    FROM memberships m
    JOIN organizations o ON o.id = m.org_id
    WHERE m.user_id = p_user_id;
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
END
$fn$
"""

_LIST_USER_ORGS_V1 = """
CREATE FUNCTION list_user_organizations(p_user_id uuid)
RETURNS TABLE(org_id uuid, name text, role text, status text)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
  PERFORM set_config('app.current_user', p_user_id::text, true);
  RETURN QUERY
    SELECT o.id, o.name::text, m.role::text, o.status::text
    FROM memberships m
    JOIN organizations o ON o.id = m.org_id
    WHERE m.user_id = p_user_id;
END
$fn$
"""


def upgrade() -> None:
    op.execute(_PROVISION_V2)
    # list_user_organizations: must DROP then CREATE (cannot CREATE OR
    # REPLACE because we keep LANGUAGE plpgsql; the existing 0051 also
    # already plpgsql, so OR REPLACE would work -- but we keep DROP for
    # symmetry with 0051 and to reset GRANTs cleanly).
    op.execute("DROP FUNCTION IF EXISTS list_user_organizations(uuid)")
    op.execute(_LIST_USER_ORGS_V2)
    op.execute("REVOKE ALL ON FUNCTION list_user_organizations(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION list_user_organizations(uuid) TO flow_app")


def downgrade() -> None:
    op.execute(_PROVISION_V1)
    op.execute("DROP FUNCTION IF EXISTS list_user_organizations(uuid)")
    op.execute(_LIST_USER_ORGS_V1)
    op.execute("REVOKE ALL ON FUNCTION list_user_organizations(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION list_user_organizations(uuid) TO flow_app")
