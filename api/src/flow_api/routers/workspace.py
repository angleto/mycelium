"""Workspace router: the user-facing name of the tenant.

Internally the tenant is still ``org`` (RLS unchanged, ADR-0015); the
rename lives in this adapter. ``/me`` is tenant-scoped. Listing and
creating workspaces is pre-tenant (authenticated by the user, no org
context yet): it powers the in-app switcher, so a user who belongs to
several workspaces never logs out to switch.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select

from flow_api.deps import TenantCtx, current_user_id, tenant_ctx
from flow_api.schemas import (
    MemberAddIn,
    MemberOut,
    MemberRoleIn,
    WorkspaceCreateIn,
    WorkspaceOut,
    WorkspacePatchIn,
    WorkspaceSettings,
    WorkspaceSettingsIn,
    WorkspaceSummaryOut,
    WorkspaceVersionOut,
)
from flow_core.concurrency import optimistic_update
from flow_core.db import admin_session
from flow_core.errors import NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.organization import Organization
from flow_core.services import memberships
from flow_core.services.auth import (
    create_org_for_user,
    delete_org_for_user,
    list_user_orgs,
    set_workspace_status,
)
from flow_core.services.rbac import ensure_role, get_role

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceSummaryOut])
async def list_my_workspaces(
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> list[WorkspaceSummaryOut]:
    """Workspaces the authenticated user belongs to (for the switcher)."""
    async with admin_session() as session:
        rows = await list_user_orgs(session, user_id=user_id)
    return [WorkspaceSummaryOut(id=r.id, name=r.name, role=r.role, status=r.status) for r in rows]


@router.post("", response_model=WorkspaceOut)
async def create_my_workspace(
    body: WorkspaceCreateIn,
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> WorkspaceOut:
    """Create an additional workspace (the caller becomes its owner).
    In-app, no re-auth."""
    async with admin_session() as session:
        ws_id = await create_org_for_user(session, user_id=user_id, name=body.name)
    return WorkspaceOut(id=ws_id, name=body.name, version=1)


@router.get("/me", response_model=WorkspaceOut)
async def get_my_workspace(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> WorkspaceOut:
    result = await ctx.session.execute(select(Organization).where(Organization.id == ctx.org_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise NotFoundError(MessageCode.ORG_NOT_FOUND)
    # my_role is the *raw* membership role (the entitlement ceiling),
    # not ctx.role (which is the possibly-downgraded effective role):
    # the SPA needs the ceiling to populate its "act as" switch. A
    # global admin acting on a workspace with no membership (sudo) is
    # reported as "owner".
    try:
        my_role = (await get_role(ctx.session, ctx.org_id, ctx.user_id)).value
    except NotFoundError:
        my_role = Role.owner.value
    return WorkspaceOut(
        id=org.id,
        name=org.name,
        version=org.version,
        settings=WorkspaceSettings.model_validate(org.settings or {}),
        my_role=my_role,
    )


@router.get("/me/members", response_model=list[MemberOut])
async def list_workspace_members(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[MemberOut]:
    """The workspace roster (any member). Powers the collaborators
    list and the SPA's "act as" switch."""
    rows = await memberships.list_members(ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id)
    return [
        MemberOut(
            user_id=m.user_id,
            email=m.email,
            display_name=m.display_name,
            role=m.role,
            created_at=m.created_at,
        )
        for m in rows
    ]


@router.post("/me/members", response_model=list[MemberOut])
async def add_workspace_member(
    body: MemberAddIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[MemberOut]:
    """Add (or re-role) a collaborator by email. Requires the effective
    role admin; the SQL re-checks (actor_id) atomically too."""
    ensure_role(ctx.role, Role.admin)
    await memberships.add_member(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        email=body.email,
        role=body.role,
    )
    return await list_workspace_members(ctx)


@router.patch("/me/members/{user_id}", response_model=list[MemberOut])
async def set_workspace_member_role(
    user_id: uuid.UUID,
    body: MemberRoleIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[MemberOut]:
    """Change a collaborator's role (effective role admin; SQL
    re-checks). Cannot demote the sole owner."""
    ensure_role(ctx.role, Role.admin)
    await memberships.set_member_role(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        target_user_id=user_id,
        role=body.role,
    )
    return await list_workspace_members(ctx)


@router.delete(
    "/me/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def remove_workspace_member(
    user_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> Response:
    """Remove a collaborator (effective role admin; SQL re-checks).
    Cannot remove the sole owner."""
    ensure_role(ctx.role, Role.admin)
    await memberships.remove_member(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        target_user_id=user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/me/settings", response_model=WorkspaceVersionOut)
async def patch_my_workspace_settings(
    body: WorkspaceSettingsIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> WorkspaceVersionOut:
    """Per-workspace config (admin). Merges into the settings bag so a
    future key is not clobbered by an estimate-presets save."""
    ensure_role(ctx.role, Role.admin)
    result = await ctx.session.execute(select(Organization).where(Organization.id == ctx.org_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise NotFoundError(MessageCode.ORG_NOT_FOUND)
    # Store the canonical Decimal string (JSONB has no decimal type):
    # round-tripping through float would render 1 as "1.0".
    merged = {
        **(org.settings or {}),
        "estimate_presets": [str(x) for x in body.estimate_presets],
    }
    new_version = await optimistic_update(
        ctx.session,
        Organization,
        pk=ctx.org_id,
        expected_version=body.expected_version,
        values={"settings": merged},
    )
    return WorkspaceVersionOut(id=ctx.org_id, version=new_version)


@router.patch("/me", response_model=WorkspaceVersionOut)
async def patch_my_workspace(
    body: WorkspacePatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> WorkspaceVersionOut:
    ensure_role(ctx.role, Role.admin)
    new_version = await optimistic_update(
        ctx.session,
        Organization,
        pk=ctx.org_id,
        expected_version=body.expected_version,
        values={"name": body.name},
    )
    return WorkspaceVersionOut(id=ctx.org_id, version=new_version)


@router.delete(
    "/{workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_my_workspace(
    workspace_id: uuid.UUID,
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> Response:
    """Hard-delete a workspace and all its tenant data (owner only;
    cannot delete the caller's only workspace). Pre-tenant: no org
    context, the switcher calls it directly."""
    async with admin_session() as session:
        await delete_org_for_user(session, user_id=user_id, org_id=workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{workspace_id}/archive",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def archive_my_workspace(
    workspace_id: uuid.UUID,
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> Response:
    """Hide a workspace from the switcher by default (owner/admin).
    Reversible via unarchive; the workspace stays fully usable."""
    async with admin_session() as session:
        await set_workspace_status(session, user_id=user_id, org_id=workspace_id, status="archived")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{workspace_id}/unarchive",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def unarchive_my_workspace(
    workspace_id: uuid.UUID,
    user_id: Annotated[uuid.UUID, Depends(current_user_id)],
) -> Response:
    """Restore an archived workspace to the default switcher view."""
    async with admin_session() as session:
        await set_workspace_status(session, user_id=user_id, org_id=workspace_id, status="active")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
