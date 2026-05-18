"""Workspace lifecycle: cascade FK + archive + guarded delete.

Org-scoped tables had no FK to ``organizations`` (org_id was a bare
indexed UUID, isolation by RLS only), so deleting a workspace would
orphan tenant data. This migration:

- adds ``org_id REFERENCES organizations(id) ON DELETE CASCADE`` to
  every org-scoped table (DB-enforced integrity; ``OrgScopedMixin``
  carries the FK for future tables);
- cleans pre-existing orphan rows (fixed-point over FK depth) so the
  constraint can be created;
- adds ``organizations.status`` ('active' | 'archived');
- replaces ``list_user_organizations`` to also return status;
- adds two SECURITY DEFINER functions (same controlled RLS-boundary
  pattern as ``provision_organization``, docs/adr/0015):
  ``delete_organization`` (owner-only, refuses the sole workspace) and
  ``set_organization_status`` (owner/admin, archive/unarchive);
- teaches ``forbid_mutation`` a transaction-local escape hatch
  (``app.allow_org_purge``) so a full-tenant delete can cascade into
  the append-only tables (activity_log, credit_ledger, usage_record).
  The append-only invariant still blocks all application/user paths;
  only the privileged owner purge, which removes the whole tenant
  including its audit trail, may bypass it.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Orphan cleanup + FK creation over every base table that has an
# ``org_id`` column. Driven off information_schema so it cannot drift
# as new org-scoped tables are added. Constraint name follows the
# SQLAlchemy naming convention (fk_<t>_org_id_organizations) so future
# autogenerate stays stable.
_CASCADE_FK = """
DO $do$
DECLARE
  r record;
  pass int;
  removed bigint;
  loop_total bigint;
  con_name text;
BEGIN
  -- 1. Drop orphan rows (org_id pointing at a non-existent org) so the
  --    FK can be added. Repeated passes converge bottom-up over the
  --    inter-table FK depth; per-table errors retry on the next pass.
  FOR pass IN 1..16 LOOP
    loop_total := 0;
    FOR r IN
      SELECT c.table_name
      FROM information_schema.columns c
      JOIN information_schema.tables t
        ON t.table_schema = c.table_schema AND t.table_name = c.table_name
      WHERE c.table_schema = 'public'
        AND c.column_name = 'org_id'
        AND t.table_type = 'BASE TABLE'
    LOOP
      BEGIN
        EXECUTE format(
          'DELETE FROM %I WHERE org_id NOT IN (SELECT id FROM organizations)',
          r.table_name
        );
        GET DIAGNOSTICS removed = ROW_COUNT;
        loop_total := loop_total + removed;
      EXCEPTION WHEN foreign_key_violation THEN
        -- a child orphan still references this row: clean it next pass
        NULL;
      END;
    END LOOP;
    EXIT WHEN loop_total = 0;
  END LOOP;

  -- 2. Add the cascade FK to every org-scoped table (idempotent).
  FOR r IN
    SELECT c.table_name
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
    WHERE c.table_schema = 'public'
      AND c.column_name = 'org_id'
      AND t.table_type = 'BASE TABLE'
  LOOP
    con_name := 'fk_' || r.table_name || '_org_id_organizations';
    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint WHERE conname = con_name
    ) THEN
      EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (org_id) '
        'REFERENCES organizations(id) ON DELETE CASCADE',
        r.table_name, con_name
      );
    END IF;
  END LOOP;
END
$do$;
"""

_DROP_CASCADE_FK = """
DO $do$
DECLARE
  r record;
  con_name text;
BEGIN
  FOR r IN
    SELECT c.table_name
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
    WHERE c.table_schema = 'public'
      AND c.column_name = 'org_id'
      AND t.table_type = 'BASE TABLE'
  LOOP
    con_name := 'fk_' || r.table_name || '_org_id_organizations';
    EXECUTE format(
      'ALTER TABLE %I DROP CONSTRAINT IF EXISTS %I', r.table_name, con_name
    );
  END LOOP;
END
$do$;
"""

# Append-only guard with a transaction-local escape hatch. Normal
# UPDATE/DELETE on activity_log/credit_ledger/usage_record stay
# forbidden; only a transaction that set app.allow_org_purge (the
# privileged org purge below) may cascade-delete their rows.
_FORBID_V2 = """
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
  IF TG_OP = 'DELETE'
     AND coalesce(current_setting('app.allow_org_purge', true), 'off') = 'on'
  THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'append-only table: % not allowed', TG_OP;
END
$fn$
"""

_FORBID_V1 = """
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
  RAISE EXCEPTION 'append-only table: % not allowed', TG_OP;
END
$fn$
"""

# SECURITY DEFINER (owner), fixed search_path; mirrors
# provision_organization / list_user_organizations (docs/adr/0015).
_DELETE_ORG = """
CREATE FUNCTION delete_organization(p_org uuid, p_user uuid)
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
  -- Allow the cascade to purge the append-only audit/ledger rows of
  -- this (about to be deleted) tenant. Transaction-local: never leaks.
  PERFORM set_config('app.allow_org_purge', 'on', true);
  -- ON DELETE CASCADE removes all org-scoped tenant data + memberships.
  DELETE FROM organizations WHERE id = p_org;
END
$fn$
"""

_SET_ORG_STATUS = """
CREATE FUNCTION set_organization_status(p_org uuid, p_user uuid, p_status text)
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

_LIST_ORGS_V2 = """
CREATE FUNCTION list_user_organizations(p_user_id uuid)
RETURNS TABLE(org_id uuid, name text, role text, status text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
  SELECT o.id, o.name, m.role::text, o.status
  FROM memberships m
  JOIN organizations o ON o.id = m.org_id
  WHERE m.user_id = p_user_id
$fn$
"""

_LIST_ORGS_V1 = """
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

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE organizations "
    "ADD COLUMN IF NOT EXISTS status varchar(16) NOT NULL DEFAULT 'active'",
    _CASCADE_FK,
    "DROP FUNCTION IF EXISTS list_user_organizations(uuid)",
    _LIST_ORGS_V2,
    "REVOKE ALL ON FUNCTION list_user_organizations(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION list_user_organizations(uuid) TO flow_app",
    _FORBID_V2,
    _DELETE_ORG,
    "REVOKE ALL ON FUNCTION delete_organization(uuid, uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION delete_organization(uuid, uuid) TO flow_app",
    _SET_ORG_STATUS,
    "REVOKE ALL ON FUNCTION set_organization_status(uuid, uuid, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION set_organization_status(uuid, uuid, text) TO flow_app",
)

DOWNGRADE: tuple[str, ...] = (
    _FORBID_V1,
    "DROP FUNCTION IF EXISTS set_organization_status(uuid, uuid, text)",
    "DROP FUNCTION IF EXISTS delete_organization(uuid, uuid)",
    "DROP FUNCTION IF EXISTS list_user_organizations(uuid)",
    _LIST_ORGS_V1,
    "REVOKE ALL ON FUNCTION list_user_organizations(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION list_user_organizations(uuid) TO flow_app",
    _DROP_CASCADE_FK,
    "ALTER TABLE organizations DROP COLUMN IF EXISTS status",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
