"""Harden the workspace role model to owner/member semantics.

Migration 0035 introduced four SECURITY DEFINER membership functions
whose actor floor was ``owner``/``admin`` and which clamped the granted
role to the actor's rank. The role model is now hardened to an
authoritative two-tier semantics (the ``admin``/``guest`` enum values
stay only for backward compatibility, they are not part of the model):

- **owner** = privileged on the namespace (manage members; modify
  clients/projects, workflows, issuer/billing profiles);
- **member** = a normal user (read/use only; NO member management, NO
  privileged writes).

Only an *owner* may manage members. A user added to a namespace later
must never be able to eject or demote the owner, nor manage members,
regardless of any forged ``X-Workspace-Role`` header (the API clamps
the effective role, and these functions re-check atomically). The
sole-owner guards are preserved so a namespace can never become
unadministrable.

This redefines (``CREATE OR REPLACE``, signatures unchanged) the three
mutating functions with the hardened guards; ``list_org_members`` is
re-emitted byte-identical to 0035 (its guard is correct: any member of
the org may read the roster). Every guard stays atomic inside the
function (defense in depth) even though the API also gates the write
with the effective role. P0001 codes still mirror the catalog
(``rbac.no_membership``, ``rbac.role_insufficient``,
``member.not_found``, ``member.last_owner``, ``member.role_invalid``)
so the service layer maps them to a typed ``DomainError`` exactly like
the workspace-lifecycle path. The downgrade ``CREATE OR REPLACE``s the
original 0035 bodies back (clean round-trip).

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---------------------------------------------------------------------------
# Hardened bodies (0036).
# ---------------------------------------------------------------------------

# Unchanged from 0035: any member of the org may read its roster (no
# role floor). Re-emitted here so the migration is self-contained and
# the downgrade has a symmetric counterpart. Same controlled
# RLS-boundary pattern as list_user_organizations.
_LIST_MEMBERS_0036 = """
CREATE OR REPLACE FUNCTION list_org_members(p_org uuid, p_user uuid)
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

# Add (or re-role) a member by email. HARDENED: the actor's membership
# role must be *exactly* ``owner``. Only an owner manages members; a
# later-added member (even one forging X-Workspace-Role: owner) is
# rejected here too. An owner may grant any valid role (including
# ``owner``), so the 0035 rank-ceiling check is removed: it is
# subsumed by the owner-only gate. Idempotent via UPSERT on the
# (org_id, user_id) unique constraint; re-adding bumps the
# optimistic-lock version.
_ADD_MEMBER_0036 = """
CREATE OR REPLACE FUNCTION add_org_member(
  p_org uuid, p_actor uuid, p_email text, p_role text
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_actor_role text;
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
  IF v_actor_role <> 'owner' THEN
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

# Change an existing member's role. HARDENED: the actor must be
# *exactly* ``owner``. An owner may set any valid role (the rank check
# is removed, subsumed by the owner-only gate). Refuses to demote the
# *sole* owner (current role 'owner', new role <> 'owner', owner count
# = 1): the org would become unadministrable. A non-owner can never
# reach the mutation; thus a later-added member can never demote the
# owner.
_SET_MEMBER_ROLE_0036 = """
CREATE OR REPLACE FUNCTION set_member_role(
  p_org uuid, p_actor uuid, p_target uuid, p_role text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_actor_role text;
  v_target_role text;
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
  IF v_actor_role <> 'owner' THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  SELECT role::text INTO v_target_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_target;
  IF v_target_role IS NULL THEN
    RAISE EXCEPTION 'member.not_found' USING ERRCODE = 'P0001';
  END IF;
  IF v_target_role = 'owner' AND p_role <> 'owner' THEN
    SELECT count(*) INTO v_owner_count
    FROM memberships
    WHERE org_id = p_org AND role = 'owner';
    IF v_owner_count = 1 THEN
      RAISE EXCEPTION 'member.last_owner' USING ERRCODE = 'P0001';
    END IF;
  END IF;
  UPDATE memberships
  SET role = p_role::role, version = version + 1
  WHERE org_id = p_org AND user_id = p_target;
END
$fn$
"""

# Remove a member. HARDENED: the actor must be *exactly* ``owner``.
# Refuses removal of the *sole* owner. Net effect: a non-owner can
# never remove anyone; an owner cannot remove the last owner; thus a
# later-added member can never eject the owner.
_REMOVE_MEMBER_0036 = """
CREATE OR REPLACE FUNCTION remove_org_member(
  p_org uuid, p_actor uuid, p_target uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
  v_actor_role text;
  v_target_role text;
  v_owner_count int;
BEGIN
  SELECT role::text INTO v_actor_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_actor;
  IF v_actor_role IS NULL THEN
    RAISE EXCEPTION 'rbac.no_membership' USING ERRCODE = 'P0001';
  END IF;
  IF v_actor_role <> 'owner' THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  SELECT role::text INTO v_target_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_target;
  IF v_target_role IS NULL THEN
    RAISE EXCEPTION 'member.not_found' USING ERRCODE = 'P0001';
  END IF;
  IF v_target_role = 'owner' THEN
    SELECT count(*) INTO v_owner_count
    FROM memberships
    WHERE org_id = p_org AND role = 'owner';
    IF v_owner_count = 1 THEN
      RAISE EXCEPTION 'member.last_owner' USING ERRCODE = 'P0001';
    END IF;
  END IF;
  DELETE FROM memberships
  WHERE org_id = p_org AND user_id = p_target;
END
$fn$
"""

# ---------------------------------------------------------------------------
# Original 0035 bodies (verbatim) for a clean downgrade.
# ---------------------------------------------------------------------------

_LIST_MEMBERS_0035 = """
CREATE OR REPLACE FUNCTION list_org_members(p_org uuid, p_user uuid)
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

_ADD_MEMBER_0035 = """
CREATE OR REPLACE FUNCTION add_org_member(
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

_SET_MEMBER_ROLE_0035 = """
CREATE OR REPLACE FUNCTION set_member_role(
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

_REMOVE_MEMBER_0035 = """
CREATE OR REPLACE FUNCTION remove_org_member(
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

# CREATE OR REPLACE preserves existing ACLs, but the REVOKE/GRANT pair
# is re-emitted (idempotent) to mirror 0035 exactly and stay correct
# even if a function were ever dropped/recreated out of band.
UPGRADE: tuple[str, ...] = (
    _LIST_MEMBERS_0036,
    "REVOKE ALL ON FUNCTION list_org_members(uuid, uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION list_org_members(uuid, uuid) TO flow_app",
    _ADD_MEMBER_0036,
    "REVOKE ALL ON FUNCTION add_org_member(uuid, uuid, text, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION add_org_member(uuid, uuid, text, text) TO flow_app",
    _SET_MEMBER_ROLE_0036,
    "REVOKE ALL ON FUNCTION set_member_role(uuid, uuid, uuid, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION set_member_role(uuid, uuid, uuid, text) TO flow_app",
    _REMOVE_MEMBER_0036,
    "REVOKE ALL ON FUNCTION remove_org_member(uuid, uuid, uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION remove_org_member(uuid, uuid, uuid) TO flow_app",
)

DOWNGRADE: tuple[str, ...] = (
    _LIST_MEMBERS_0035,
    "REVOKE ALL ON FUNCTION list_org_members(uuid, uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION list_org_members(uuid, uuid) TO flow_app",
    _ADD_MEMBER_0035,
    "REVOKE ALL ON FUNCTION add_org_member(uuid, uuid, text, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION add_org_member(uuid, uuid, text, text) TO flow_app",
    _SET_MEMBER_ROLE_0035,
    "REVOKE ALL ON FUNCTION set_member_role(uuid, uuid, uuid, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION set_member_role(uuid, uuid, uuid, text) TO flow_app",
    _REMOVE_MEMBER_0035,
    "REVOKE ALL ON FUNCTION remove_org_member(uuid, uuid, uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION remove_org_member(uuid, uuid, uuid) TO flow_app",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
