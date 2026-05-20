"""Make list_user_organizations portable: no BYPASSRLS required.

Companion to migration 0050. The 0014 ``list_user_organizations`` is
SECURITY DEFINER (LANGUAGE sql) but its body queries the FORCE-RLS
``memberships`` and ``organizations`` tables, whose policies key on
``app.current_org``. On a managed Postgres without BYPASSRLS for the
function owner (``flow``), the function returns 0 rows even when the
user has real memberships -- because the SELECT is filtered by RLS,
and the function does not set any tenant GUC before reading.

This breaks the post-login flow: ``GET /workspaces`` calls
``list_user_organizations(<jwt user>)``, gets ``[]``, and the SPA
throws "Something went wrong" because it cannot pick a workspace.

The portable fix has two parts (same spirit as 0050: do not require
BYPASSRLS, just shape the query so the existing RLS evaluates true):

1. Add two *additional* permissive RLS policies that let a principal
   read THEIR OWN membership rows (and the organizations they are a
   member of) regardless of the tenant GUC. Tenant writes / scoped
   reads stay unchanged: the existing ``p_memberships`` /
   ``p_organizations`` policies still apply OR-wise with these.

2. Rewrite ``list_user_organizations`` in plpgsql so it sets
   ``app.current_user`` transaction-local (``set_config(.., true)``)
   before the SELECT. The two new policies then evaluate true for the
   rows being scanned.

Revision: 0051
Down revision: 0050
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


_USER = "nullif(current_setting('app.current_user', true), '')::uuid"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE POLICY p_memberships_self_read ON memberships
          FOR SELECT
          USING (user_id = {_USER})
        """
    )
    op.execute(
        f"""
        CREATE POLICY p_organizations_self_read ON organizations
          FOR SELECT
          USING (
            id IN (
              SELECT m.org_id FROM memberships m
              WHERE m.user_id = {_USER}
            )
          )
        """
    )
    # DROP first: PG refuses to change LANGUAGE (sql -> plpgsql) via
    # CREATE OR REPLACE. The 0014 GRANT/REVOKE bindings need to be
    # re-established after re-create.
    op.execute("DROP FUNCTION IF EXISTS list_user_organizations(uuid)")
    op.execute(
        """
        CREATE FUNCTION list_user_organizations(p_user_id uuid)
        RETURNS TABLE(org_id uuid, name text, role text)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        BEGIN
          PERFORM set_config('app.current_user', p_user_id::text, true);
          RETURN QUERY
            SELECT o.id, o.name::text, m.role::text
            FROM memberships m
            JOIN organizations o ON o.id = m.org_id
            WHERE m.user_id = p_user_id;
        END
        $fn$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION list_user_organizations(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION list_user_organizations(uuid) TO flow_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS list_user_organizations(uuid)")
    op.execute(
        """
        CREATE FUNCTION list_user_organizations(p_user_id uuid)
        RETURNS TABLE(org_id uuid, name text, role text)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
          SELECT o.id, o.name, m.role::text
          FROM memberships m
          JOIN organizations o ON o.id = m.org_id
          WHERE m.user_id = p_user_id
        $fn$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION list_user_organizations(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION list_user_organizations(uuid) TO flow_app")
    op.execute("DROP POLICY IF EXISTS p_organizations_self_read ON organizations")
    op.execute("DROP POLICY IF EXISTS p_memberships_self_read ON memberships")
