"""Admin router: global user administration. Gated by ``require_admin``
(a property of the user, not a workspace role). ``users`` is not
RLS-scoped, so these use an admin session and never touch tenant data.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from flow_api.deps import require_admin
from flow_api.schemas import AdminUserOut, AdminUserPatchIn
from flow_core.db import admin_session
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.user import User

router = APIRouter(prefix="/admin/users", tags=["admin"])


def _out(u: User) -> AdminUserOut:
    return AdminUserOut(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        is_admin=u.is_admin,
        is_active=u.is_active,
        email_verified=u.email_verified_at is not None,
        mfa_enabled=u.mfa_enabled_at is not None,
        created_at=u.created_at,
    )


@router.get("", response_model=list[AdminUserOut])
async def list_users(
    _admin: Annotated[User, Depends(require_admin)],
) -> list[AdminUserOut]:
    async with admin_session() as s:
        rows = (await s.execute(select(User).order_by(User.created_at))).scalars().all()
    return [_out(u) for u in rows]


@router.patch("/{user_id}", response_model=AdminUserOut)
async def patch_user(
    user_id: uuid.UUID,
    body: AdminUserPatchIn,
    admin: Annotated[User, Depends(require_admin)],
) -> AdminUserOut:
    """Toggle admin / activation. An admin cannot strip their own admin
    role or deactivate themselves (lock-out / orphaned-admin guard);
    another admin must do it."""
    async with admin_session() as s:
        u = (await s.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if u is None:
            raise NotFoundError(MessageCode.USER_NOT_FOUND)
        demote_self = u.id == admin.id and (body.is_admin is False or body.is_active is False)
        if demote_self:
            raise DomainError(MessageCode.ADMIN_SELF_GUARD)
        if body.is_admin is not None:
            u.is_admin = body.is_admin
        if body.is_active is not None:
            u.is_active = body.is_active
        await s.flush()
        out = _out(u)
    return out
