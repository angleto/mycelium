"""Workspace membership management (collaborators + their roles).

The user-facing concept is "workspace"; internally the tenant is still
``org`` (RLS unchanged, ADR-0015). This mirrors
``auth.delete_org_for_user`` exactly: the RLS boundary is crossed only
through SECURITY DEFINER functions (migration 0035); the precondition
(actor membership, actor role floor, sole-owner, target existence) is
pre-checked here in Python so the caller gets a typed ``DomainError``
with a stable ``MessageCode``, and the same guard is re-checked
atomically inside the SQL function (defense in depth, identical to the
workspace-lifecycle path).

Rank order matches ``rbac._RANK``: owner > admin > member > guest.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError, ForbiddenError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.user import User
from flow_core.services.rbac import _RANK

_VALID_ROLES = {r.value for r in Role}
_MANAGER_ROLES = {Role.owner.value, Role.admin.value}


@dataclass(frozen=True, slots=True)
class Member:
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    created_at: dt.datetime


async def list_members(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID
) -> list[Member]:
    """The org roster (any member may read it). Crosses the RLS boundary
    only through the SECURITY DEFINER ``list_org_members`` function
    (migration 0035), which re-checks the actor's membership atomically
    (defense in depth)."""
    rows = await session.execute(
        text("SELECT user_id, email, display_name, role, created_at FROM list_org_members(:o, :a)"),
        {"o": str(org_id), "a": str(actor_id)},
    )
    return [
        Member(
            user_id=uuid.UUID(str(r.user_id)),
            email=str(r.email),
            display_name=r.display_name,
            role=str(r.role),
            created_at=r.created_at,
        )
        for r in rows
    ]


def _require_manager(members: list[Member], actor_id: uuid.UUID) -> str:
    """The actor must be a member and owner/admin to manage members.
    Returns the actor's role (for the rank check)."""
    actor = next((m for m in members if m.user_id == actor_id), None)
    if actor is None:
        raise ForbiddenError(MessageCode.RBAC_NO_MEMBERSHIP)
    if actor.role not in _MANAGER_ROLES:
        raise ForbiddenError(
            MessageCode.RBAC_ROLE_INSUFFICIENT,
            current=actor.role,
            minimum=Role.admin.value,
        )
    return actor.role


def _validate_role(role: str) -> None:
    if role not in _VALID_ROLES:
        raise DomainError(MessageCode.MEMBER_ROLE_INVALID)


def _ensure_not_above(actor_role: str, target_role: str) -> None:
    """An owner/admin cannot grant a role above their own rank (an admin
    cannot mint an owner)."""
    if _RANK[Role(actor_role)] < _RANK[Role(target_role)]:
        raise ForbiddenError(
            MessageCode.RBAC_ROLE_INSUFFICIENT,
            current=actor_role,
            minimum=target_role,
        )


def _is_sole_owner(members: list[Member], target_user_id: uuid.UUID) -> bool:
    owners = [m for m in members if m.role == Role.owner.value]
    return len(owners) == 1 and owners[0].user_id == target_user_id


async def add_member(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    email: str,
    role: str,
) -> uuid.UUID:
    """Add (or re-role) a collaborator by email. The actor must be
    owner/admin and cannot grant above their own rank. Routed through
    the SECURITY DEFINER ``add_org_member`` function (migration 0035),
    which re-checks every precondition atomically."""
    _validate_role(role)
    members = await list_members(session, org_id=org_id, actor_id=actor_id)
    actor_role = _require_manager(members, actor_id)
    _ensure_not_above(actor_role, role)
    # Pre-resolve the target by email so the caller gets a typed
    # NotFoundError (the SQL re-resolves and RAISEs the same code as
    # defense in depth, exactly like auth.delete_org_for_user). users
    # is global (not RLS-scoped): the lookup is valid under the tenant
    # session.
    target = (
        await session.execute(select(User).where(func.lower(User.email) == email.lower()))
    ).scalar_one_or_none()
    if target is None:
        raise NotFoundError(MessageCode.MEMBER_NOT_FOUND)
    result = await session.execute(
        text("SELECT add_org_member(:o, :a, :e, :r)"),
        {"o": str(org_id), "a": str(actor_id), "e": email, "r": role},
    )
    return uuid.UUID(str(result.scalar_one()))


async def set_member_role(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role: str,
) -> None:
    """Change a member's role (owner/admin; cannot grant above own rank;
    cannot demote the sole owner). Routed through the SECURITY DEFINER
    ``set_member_role`` function (migration 0035)."""
    _validate_role(role)
    members = await list_members(session, org_id=org_id, actor_id=actor_id)
    actor_role = _require_manager(members, actor_id)
    _ensure_not_above(actor_role, role)
    target = next((m for m in members if m.user_id == target_user_id), None)
    if target is None:
        raise NotFoundError(MessageCode.MEMBER_NOT_FOUND)
    if role != Role.owner.value and _is_sole_owner(members, target_user_id):
        raise DomainError(MessageCode.MEMBER_LAST_OWNER)
    await session.execute(
        text("SELECT set_member_role(:o, :a, :t, :r)"),
        {
            "o": str(org_id),
            "a": str(actor_id),
            "t": str(target_user_id),
            "r": role,
        },
    )


async def remove_member(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> None:
    """Remove a collaborator (owner/admin; cannot remove the sole
    owner). Routed through the SECURITY DEFINER ``remove_org_member``
    function (migration 0035)."""
    members = await list_members(session, org_id=org_id, actor_id=actor_id)
    _require_manager(members, actor_id)
    target = next((m for m in members if m.user_id == target_user_id), None)
    if target is None:
        raise NotFoundError(MessageCode.MEMBER_NOT_FOUND)
    if _is_sole_owner(members, target_user_id):
        raise DomainError(MessageCode.MEMBER_LAST_OWNER)
    await session.execute(
        text("SELECT remove_org_member(:o, :a, :t)"),
        {"o": str(org_id), "a": str(actor_id), "t": str(target_user_id)},
    )
