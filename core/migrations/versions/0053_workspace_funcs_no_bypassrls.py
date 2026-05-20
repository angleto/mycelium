"""delete_organization + set_organization_status: portable to managed
Postgres (no BYPASSRLS).

The 0019 ``delete_organization`` and ``set_organization_status`` are
SECURITY DEFINER plpgsql functions that query the FORCE-RLS
``memberships`` and ``organizations`` tables without setting the
tenant GUC. On managed Postgres (Scaleway) the function owner does
not have BYPASSRLS, so the SELECTs see zero rows and the functions
raise ``workspace.not_owner`` even when the caller IS the owner of
the target workspace; from the SPA this surfaces as a generic 500.

Mirrors the fix shipped in 0050 (``provision_organization``) and
0051/0052 (``list_user_organizations``): set the right GUCs
transaction-local at function entry so the existing RLS evaluates
true for the scoped queries, restore the caller's previous GUCs
before returning.

The membership-count guard (``<= 1`` memberships across ALL orgs)
relies on the self-read policy ``p_memberships_self_read`` added in
0051, which keys on ``app.current_user`` and is org-agnostic.

Revision: 0053
Down revision: 0052
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


_DELETE_ORG_V2 = """
CREATE OR REPLACE FUNCTION delete_organization(p_org uuid, p_user uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_prev_org text := current_setting('app.current_org', true);
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  -- Set GUCs so the FORCE-RLS policies on memberships / organizations
  -- evaluate true for the scoped reads + the org DELETE. Without this
  -- the SECURITY DEFINER body sees zero rows on managed Postgres.
  PERFORM set_config('app.current_org', p_org::text, true);
  PERFORM set_config('app.current_user', p_user::text, true);
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_user AND role = 'owner'
  ) THEN
    PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
    PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
    RAISE EXCEPTION 'workspace.not_owner' USING ERRCODE = 'P0001';
  END IF;
  -- Count ALL memberships for the user (across orgs). The
  -- p_memberships_self_read policy (from 0051) allows this regardless
  -- of which org the GUC currently points at, since it keys on
  -- app.current_user.
  IF (SELECT count(*) FROM memberships WHERE user_id = p_user) <= 1 THEN
    PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
    PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
    RAISE EXCEPTION 'workspace.sole' USING ERRCODE = 'P0001';
  END IF;
  -- Allow the cascade to purge the append-only audit/ledger rows of
  -- this (about to be deleted) tenant. Transaction-local: never leaks.
  PERFORM set_config('app.allow_org_purge', 'on', true);
  -- ON DELETE CASCADE removes all org-scoped tenant data + memberships.
  DELETE FROM organizations WHERE id = p_org;
  PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
END
$fn$
"""

_DELETE_ORG_V1 = """
CREATE OR REPLACE FUNCTION delete_organization(p_org uuid, p_user uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_user AND role = 'owner'
  ) THEN
    RAISE EXCEPTION 'workspace.not_owner' USING ERRCODE = 'P0001';
  END IF;
  IF (SELECT count(*) FROM memberships WHERE user_id = p_user) <= 1 THEN
    RAISE EXCEPTION 'workspace.sole' USING ERRCODE = 'P0001';
  END IF;
  PERFORM set_config('app.allow_org_purge', 'on', true);
  DELETE FROM organizations WHERE id = p_org;
END
$fn$
"""


_SET_ORG_STATUS_V2 = """
CREATE OR REPLACE FUNCTION set_organization_status(p_org uuid, p_user uuid, p_status text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_prev_org text := current_setting('app.current_org', true);
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  IF p_status NOT IN ('active', 'archived') THEN
    RAISE EXCEPTION 'workspace.bad_status' USING ERRCODE = 'P0001';
  END IF;
  PERFORM set_config('app.current_org', p_org::text, true);
  PERFORM set_config('app.current_user', p_user::text, true);
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_user
      AND role IN ('owner', 'admin')
  ) THEN
    PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
    PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
    RAISE EXCEPTION 'workspace.not_owner' USING ERRCODE = 'P0001';
  END IF;
  UPDATE organizations
  SET status = p_status, version = version + 1
  WHERE id = p_org;
  PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
END
$fn$
"""

_SET_ORG_STATUS_V1 = """
CREATE OR REPLACE FUNCTION set_organization_status(p_org uuid, p_user uuid, p_status text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
  IF p_status NOT IN ('active', 'archived') THEN
    RAISE EXCEPTION 'workspace.bad_status' USING ERRCODE = 'P0001';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_user
      AND role IN ('owner', 'admin')
  ) THEN
    RAISE EXCEPTION 'workspace.not_owner' USING ERRCODE = 'P0001';
  END IF;
  UPDATE organizations
  SET status = p_status, version = version + 1
  WHERE id = p_org;
END
$fn$
"""


def upgrade() -> None:
    op.execute(_DELETE_ORG_V2)
    op.execute(_SET_ORG_STATUS_V2)


def downgrade() -> None:
    op.execute(_DELETE_ORG_V1)
    op.execute(_SET_ORG_STATUS_V1)
