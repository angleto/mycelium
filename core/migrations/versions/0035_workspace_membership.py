"""Workspace membership management: list/add/role/remove members.

The tenant has a user-facing name ("workspace"); internally it is still
``org`` (RLS unchanged, ADR-0015). Membership is the RBAC edge
(``memberships``: org_id + user_id + role). This migration adds four
SECURITY DEFINER functions that cross the RLS boundary in a controlled
way, exactly like ``provision_organization`` /
``list_user_organizations`` / ``delete_organization``
(docs/adr/0015, migrations 0001/0014/0019):

- ``list_org_members`` (any member of the org may read its roster);
- ``add_org_member`` (owner/admin, cannot grant above own rank);
- ``set_member_role`` (owner/admin, cannot demote the sole owner);
- ``remove_org_member`` (owner/admin, cannot remove the sole owner).

Every guard is re-checked atomically inside the function (defense in
depth) even though the API also gates the write with the effective
role. P0001 codes mirror the catalog (rbac.no_membership,
rbac.role_insufficient, member.not_found, member.last_owner,
member.role_invalid) so the service layer maps them to typed
DomainError exactly like the workspace-lifecycle path.

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Any member of the org can read its roster (no role floor): the
# switcher / settings page lists collaborators. Actor must have *some*
# membership in p_org. SECURITY DEFINER (owner), fixed search_path;
# same controlled RLS-boundary pattern as list_user_organizations.
_LIST_MEMBERS = """
CREATE FUNCTION list_org_members(p_org uuid, p_user uuid)
RETURNS TABLE(
  user_id uuid,
  email text,
  display_name text,
  role text,
  created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
-- The RETURNS TABLE OUT names (user_id, role, created_at, ...) shadow
-- the like-named memberships/users columns inside plpgsql; resolve any
-- ambiguous reference to the column, never the OUT variable.
#variable_conflict use_column
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_user
  ) THEN
    RAISE EXCEPTION 'rbac.no_membership' USING ERRCODE = 'P0001';
  END IF;
  RETURN QUERY
  SELECT
    u.id,
    u.email::text,
    u.display_name::text,
    m.role::text,
    m.created_at
  FROM memberships m
  JOIN users u ON u.id = m.user_id
  WHERE m.org_id = p_org
  ORDER BY m.created_at;
END
$fn$
"""

# Add (or re-role) a member by email. The actor must be owner/admin and
# cannot grant a role above their own rank (an admin cannot mint an
# owner). Idempotent via UPSERT on the (org_id, user_id) unique
# constraint; re-adding bumps the optimistic-lock version.
_ADD_MEMBER = """
CREATE FUNCTION add_org_member(
  p_org uuid, p_actor uuid, p_email text, p_role text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_actor_role text;
  v_actor_rank int;
  v_target_rank int;
  v_target uuid;
BEGIN
  IF p_role NOT IN ('owner', 'admin', 'member', 'guest') THEN
    RAISE EXCEPTION 'member.role_invalid' USING ERRCODE = 'P0001';
  END IF;
  SELECT role::text INTO v_actor_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_actor;
  IF v_actor_role IS NULL THEN
    RAISE EXCEPTION 'rbac.no_membership' USING ERRCODE = 'P0001';
  END IF;
  IF v_actor_role NOT IN ('owner', 'admin') THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  v_actor_rank := CASE v_actor_role
    WHEN 'owner' THEN 3 WHEN 'admin' THEN 2
    WHEN 'member' THEN 1 ELSE 0 END;
  v_target_rank := CASE p_role
    WHEN 'owner' THEN 3 WHEN 'admin' THEN 2
    WHEN 'member' THEN 1 ELSE 0 END;
  IF v_actor_rank < v_target_rank THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  SELECT id INTO v_target FROM users WHERE lower(email) = lower(p_email);
  IF v_target IS NULL THEN
    RAISE EXCEPTION 'member.not_found' USING ERRCODE = 'P0001';
  END IF;
  INSERT INTO memberships (org_id, user_id, role)
  VALUES (p_org, v_target, p_role::role)
  ON CONFLICT (org_id, user_id)
  DO UPDATE SET role = excluded.role, version = memberships.version + 1;
  RETURN v_target;
END
$fn$
"""

# Change an existing member's role. Same actor checks as add. Refuses
# to demote the *sole* owner (the org would become unadministrable).
_SET_MEMBER_ROLE = """
CREATE FUNCTION set_member_role(
  p_org uuid, p_actor uuid, p_target uuid, p_role text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_actor_role text;
  v_actor_rank int;
  v_target_rank int;
  v_owner_count int;
BEGIN
  IF p_role NOT IN ('owner', 'admin', 'member', 'guest') THEN
    RAISE EXCEPTION 'member.role_invalid' USING ERRCODE = 'P0001';
  END IF;
  SELECT role::text INTO v_actor_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_actor;
  IF v_actor_role IS NULL THEN
    RAISE EXCEPTION 'rbac.no_membership' USING ERRCODE = 'P0001';
  END IF;
  IF v_actor_role NOT IN ('owner', 'admin') THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  v_actor_rank := CASE v_actor_role
    WHEN 'owner' THEN 3 WHEN 'admin' THEN 2
    WHEN 'member' THEN 1 ELSE 0 END;
  v_target_rank := CASE p_role
    WHEN 'owner' THEN 3 WHEN 'admin' THEN 2
    WHEN 'member' THEN 1 ELSE 0 END;
  IF v_actor_rank < v_target_rank THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_target
  ) THEN
    RAISE EXCEPTION 'member.not_found' USING ERRCODE = 'P0001';
  END IF;
  SELECT count(*) INTO v_owner_count
  FROM memberships
  WHERE org_id = p_org AND role = 'owner';
  IF v_owner_count = 1
     AND p_role <> 'owner'
     AND EXISTS (
       SELECT 1 FROM memberships
       WHERE org_id = p_org AND user_id = p_target AND role = 'owner'
     )
  THEN
    RAISE EXCEPTION 'member.last_owner' USING ERRCODE = 'P0001';
  END IF;
  UPDATE memberships
  SET role = p_role::role, version = version + 1
  WHERE org_id = p_org AND user_id = p_target;
END
$fn$
"""

# Remove a member. Owner/admin only; refuses removal of the sole owner.
_REMOVE_MEMBER = """
CREATE FUNCTION remove_org_member(
  p_org uuid, p_actor uuid, p_target uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_actor_role text;
  v_owner_count int;
BEGIN
  SELECT role::text INTO v_actor_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_actor;
  IF v_actor_role IS NULL THEN
    RAISE EXCEPTION 'rbac.no_membership' USING ERRCODE = 'P0001';
  END IF;
  IF v_actor_role NOT IN ('owner', 'admin') THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_target
  ) THEN
    RAISE EXCEPTION 'member.not_found' USING ERRCODE = 'P0001';
  END IF;
  SELECT count(*) INTO v_owner_count
  FROM memberships
  WHERE org_id = p_org AND role = 'owner';
  IF v_owner_count = 1
     AND EXISTS (
       SELECT 1 FROM memberships
       WHERE org_id = p_org AND user_id = p_target AND role = 'owner'
     )
  THEN
    RAISE EXCEPTION 'member.last_owner' USING ERRCODE = 'P0001';
  END IF;
  DELETE FROM memberships
  WHERE org_id = p_org AND user_id = p_target;
END
$fn$
"""

UPGRADE: tuple[str, ...] = (
    _LIST_MEMBERS,
    "REVOKE ALL ON FUNCTION list_org_members(uuid, uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION list_org_members(uuid, uuid) TO flow_app",
    _ADD_MEMBER,
    "REVOKE ALL ON FUNCTION add_org_member(uuid, uuid, text, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION add_org_member(uuid, uuid, text, text) TO flow_app",
    _SET_MEMBER_ROLE,
    "REVOKE ALL ON FUNCTION set_member_role(uuid, uuid, uuid, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION set_member_role(uuid, uuid, uuid, text) TO flow_app",
    _REMOVE_MEMBER,
    "REVOKE ALL ON FUNCTION remove_org_member(uuid, uuid, uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION remove_org_member(uuid, uuid, uuid) TO flow_app",
)

DOWNGRADE: tuple[str, ...] = (
    "DROP FUNCTION IF EXISTS remove_org_member(uuid, uuid, uuid)",
    "DROP FUNCTION IF EXISTS set_member_role(uuid, uuid, uuid, text)",
    "DROP FUNCTION IF EXISTS add_org_member(uuid, uuid, text, text)",
    "DROP FUNCTION IF EXISTS list_org_members(uuid, uuid)",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
