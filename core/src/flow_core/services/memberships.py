"""Workspace membership management (collaborators + their roles).

The user-facing concept is "workspace"; internally the tenant is still
``org`` (RLS unchanged, ADR-0015). This mirrors
``auth.delete_org_for_user`` exactly: the RLS boundary is crossed only
through SECURITY DEFINER functions (migrations 0035/0036); the
precondition (actor membership, actor role, sole-owner, target
existence) is pre-checked here in Python so the caller gets a typed
``DomainError`` with a stable ``MessageCode``, and the same guard is
re-checked atomically inside the SQL function (defense in depth,
identical to the workspace-lifecycle path).

Hardened role model (migration 0036, authoritative): only an
**owner** manages members; a **member** is a normal user with no
member-management capability. The actor's membership role must be
*exactly* ``owner`` to add/re-role/remove a collaborator. The
``admin``/``guest`` enum values are kept only for backward
compatibility and are not part of this model (anything that is not
``owner`` is treated as a normal user). The sole-owner guards are
preserved so a namespace can never become unadministrable, and so a
later-added member can never eject or demote the owner.
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

_VALID_ROLES = {r.value for r in Role}


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


def _require_owner(members: list[Member], actor_id: uuid.UUID) -> None:
    """Only an owner manages members (hardened model, migration 0036).
    The actor must have a membership in the workspace AND that
    membership role must be *exactly* ``owner``: a normal member (or a
    legacy ``admin``/``guest``) can never add, re-role or remove a
    collaborator, regardless of any forged ``X-Workspace-Role`` header
    (the API also clamps the effective role; the SQL re-checks
    atomically as defense in depth)."""
    actor = next((m for m in members if m.user_id == actor_id), None)
    if actor is None:
        raise ForbiddenError(MessageCode.RBAC_NO_MEMBERSHIP)
    if actor.role != Role.owner.value:
        raise ForbiddenError(
            MessageCode.RBAC_ROLE_INSUFFICIENT,
            current=actor.role,
            minimum=Role.owner.value,
        )


def _validate_role(role: str) -> None:
    if role not in _VALID_ROLES:
        raise DomainError(MessageCode.MEMBER_ROLE_INVALID)


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
    """Add (or re-role) a collaborator by email. The actor's
    membership role must be *exactly* ``owner`` (hardened model,
    migration 0036): only an owner manages members. An owner may grant
    any valid role. Routed through the SECURITY DEFINER
    ``add_org_member`` function, which re-checks every precondition
    atomically."""
    _validate_role(role)
    members = await list_members(session, org_id=org_id, actor_id=actor_id)
    _require_owner(members, actor_id)
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
    """Change a member's role. The actor must be *exactly* ``owner``
    (hardened model, migration 0036); an owner may set any valid role.
    Refuses to demote the sole owner (a later-added member can never
    reach this code, so the owner is also protected from being demoted
    by anyone else). Routed through the SECURITY DEFINER
    ``set_member_role`` function."""
    _validate_role(role)
    members = await list_members(session, org_id=org_id, actor_id=actor_id)
    _require_owner(members, actor_id)
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
    """Remove a collaborator. The actor must be *exactly* ``owner``
    (hardened model, migration 0036); cannot remove the sole owner.
    Net effect: a non-owner can never remove anyone and the last owner
    is irremovable, so a later-added member can never eject the owner.
    Routed through the SECURITY DEFINER ``remove_org_member``
    function."""
    members = await list_members(session, org_id=org_id, actor_id=actor_id)
    _require_owner(members, actor_id)
    target = next((m for m in members if m.user_id == target_user_id), None)
    if target is None:
        raise NotFoundError(MessageCode.MEMBER_NOT_FOUND)
    if _is_sole_owner(members, target_user_id):
        raise DomainError(MessageCode.MEMBER_LAST_OWNER)
    await session.execute(
        text("SELECT remove_org_member(:o, :a, :t)"),
        {"o": str(org_id), "a": str(actor_id), "t": str(target_user_id)},
    )
