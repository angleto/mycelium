"""Example org-scoped router: demonstrates RLS isolation and optimistic
concurrency end-to-end (docs/adr/0002)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import OrgOut, OrgPatchIn, OrgVersionOut
from flow_core.concurrency import optimistic_update
from flow_core.errors import NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.organization import Organization
from flow_core.services.rbac import ensure_role

router = APIRouter(prefix="/orgs", tags=["orgs"])


@router.get("/me", response_model=OrgOut)
async def get_my_org(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> OrgOut:
    result = await ctx.session.execute(select(Organization).where(Organization.id == ctx.org_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise NotFoundError(MessageCode.ORG_NOT_FOUND)
    return OrgOut(id=org.id, name=org.name, version=org.version)


@router.patch("/me", response_model=OrgVersionOut)
async def patch_my_org(
    body: OrgPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> OrgVersionOut:
    ensure_role(ctx.role, Role.admin)
    new_version = await optimistic_update(
        ctx.session,
        Organization,
        pk=ctx.org_id,
        expected_version=body.expected_version,
        values={"name": body.name},
    )
    return OrgVersionOut(id=ctx.org_id, version=new_version)
