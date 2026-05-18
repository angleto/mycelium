"""W1a: list the workspaces a user belongs to (pre-tenant selection).

Pure read, SECURITY DEFINER (owner), fixed search_path, same controlled
RLS-boundary pattern as ``provision_organization`` (docs/adr/0015). The
user-facing concept is "workspace"; internally the tenant is still
``org`` (RLS unchanged). Additive: no schema change.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
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
    """,
    "REVOKE ALL ON FUNCTION list_user_organizations(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION list_user_organizations(uuid) TO flow_app",
)

DOWNGRADE: tuple[str, ...] = ("DROP FUNCTION IF EXISTS list_user_organizations(uuid)",)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
