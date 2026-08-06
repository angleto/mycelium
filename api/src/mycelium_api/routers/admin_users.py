"""Admin router: global user administration. Gated by ``require_admin``
(a property of the user, not a workspace role). ``users`` is not
RLS-scoped, so these use an admin session and never touch tenant data.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import load_only

from mycelium_api.deps import require_admin
from mycelium_api.schemas import AdminUserOut, AdminUserPatchIn
from mycelium_core.db import admin_session
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.user import User

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
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AdminUserOut]:
    """Users, newest first, one bounded page at a time.

    ``q`` is a case-insensitive substring of the email or the display
    name. Search is the point, not paging: this table grows without
    limit and an admin's job here is "find one person, toggle a flag",
    which walking pages does not serve. Before this was bounded the
    handler selected EVERY user row into one response.

    The sort key is ``(created_at, id)``, not ``created_at`` alone:
    accounts get created in the same clock tick (the test suite signs
    one up per test), and a non-total ordering makes ``offset`` drop and
    repeat rows across pages.
    """
    stmt = (
        select(User)
        # ``_out`` reads eight columns; a bare ``select(User)`` would
        # also haul password_hash, mfa_secret and backup_codes_hash out
        # of the database for every row on the page.
        .options(
            load_only(
                User.email,
                User.display_name,
                User.is_admin,
                User.is_active,
                User.email_verified_at,
                User.mfa_enabled_at,
                User.created_at,
            )
        )
        .order_by(User.created_at.desc(), User.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(or_(User.email.ilike(pattern), User.display_name.ilike(pattern)))
    async with admin_session() as s:
        rows = (await s.execute(stmt)).scalars().all()
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
