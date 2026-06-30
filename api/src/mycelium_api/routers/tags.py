"""Tags / clients / projects router. Thin adapter over the service
layer (docs/adr/0001, 0003)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from mycelium_api.deps import TenantCtx, tenant_ctx
from mycelium_api.schemas import (
    ClientCreateIn,
    ClientOut,
    ClientPatchIn,
    ProjectCreateIn,
    ProjectOut,
    ProjectPatchIn,
    TagCreateIn,
    TagOut,
    TagPatchIn,
    TagScopeIn,
    VersionOut,
)
from mycelium_core.models.membership import Role
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.services import taxonomy
from mycelium_core.services.rbac import ensure_role
from mycelium_core.services.taxonomy import ClientInput

router = APIRouter(tags=["taxonomy"])


def _out(tag: Tag, scope: list[uuid.UUID] | None = None) -> TagOut:
    return TagOut(
        id=tag.id,
        kind=tag.kind,
        name=tag.name,
        color=tag.color,
        status=tag.status,
        scope_target_ids=scope or [],
        version=tag.version,
    )


@router.post("/tags", response_model=TagOut)
async def create_tag(
    body: TagCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> TagOut:
    tag = await taxonomy.create_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        kind=body.kind,
        name=body.name,
        color=body.color,
    )
    return _out(tag)


@router.get("/tags", response_model=list[TagOut])
async def list_tags(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    kind: TagKind | None = None,
    for_project: uuid.UUID | None = None,
    for_client: uuid.UUID | None = None,
    include_archived: bool = False,
    manage: bool = False,
) -> list[TagOut]:
    """Archived tags are excluded by default so they vanish from every
    selection/filter surface; the Tag manager passes
    ``include_archived=true`` to still un-archive one. ``for_project`` /
    ``for_client`` scope the list to the SPA's current focus (global +
    in-scope tags only). ``manage=true`` marks the Tag-manager surface:
    under a focus it still surfaces GLOBAL generic tags (no scope rows)
    so an unrestricted tag stays reachable to add a "Restrict to..." —
    filter/selection surfaces keep the stricter focus rule."""
    tags = await taxonomy.list_tags(
        ctx.session,
        org_id=ctx.org_id,
        kind=kind,
        for_project=for_project,
        for_client=for_client,
        include_archived=include_archived,
        manage=manage,
    )
    scopes = await taxonomy.scopes_by_tag(ctx.session, tag_ids=[t.id for t in tags])
    return [_out(t, scopes.get(t.id, [])) for t in tags]


@router.put("/tags/{tag_id}/scope", status_code=204)
async def set_tag_scope(
    tag_id: uuid.UUID,
    body: TagScopeIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    ensure_role(ctx.role, Role.owner)
    await taxonomy.set_tag_scope(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        tag_id=tag_id,
        target_ids=body.target_ids,
    )


@router.post("/clients", response_model=TagOut)
async def create_client(
    body: ClientCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> TagOut:
    ensure_role(ctx.role, Role.owner)
    profile = ClientInput(
        legal_name=body.legal_name,
        first_name=body.first_name,
        last_name=body.last_name,
        country_code=body.country_code,
        vat_number=body.vat_number,
        tax_code=body.tax_code,
        address=body.address,
        civic_number=body.civic_number,
        postal_code=body.postal_code,
        city=body.city,
        province=body.province,
        country=body.country,
        sdi_code=body.sdi_code,
        pec=body.pec,
        invoice_series=body.invoice_series,
        payment_iban=body.payment_iban,
        description=body.description,
        default_billable=body.default_billable,
        hourly_rate=body.hourly_rate,
        currency=body.currency,
        timezone=body.timezone,
        default_payment_conditions_code=body.default_payment_conditions_code,
        default_payment_method_code=body.default_payment_method_code,
        default_payment_terms_days=body.default_payment_terms_days,
        invoice_language=body.invoice_language,
        invoice_date_format=body.invoice_date_format,
    )
    tag = await taxonomy.create_client(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        name=body.name,
        profile=profile,
    )
    return _out(tag)


@router.post("/projects", response_model=TagOut)
async def create_project(
    body: ProjectCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> TagOut:
    ensure_role(ctx.role, Role.owner)
    tag = await taxonomy.create_project(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        name=body.name,
        client_tag_id=body.client_tag_id,
        budget=body.budget,
        color=body.color,
        description=body.description,
    )
    return _out(tag)


def _client_out(t: Tag, p: object) -> ClientOut:
    return ClientOut(
        id=t.id,
        name=t.name,
        status=t.status,
        version=t.version,
        legal_name=p.legal_name,  # type: ignore[attr-defined]
        first_name=p.first_name,  # type: ignore[attr-defined]
        last_name=p.last_name,  # type: ignore[attr-defined]
        country_code=p.country_code,  # type: ignore[attr-defined]
        vat_number=p.vat_number,  # type: ignore[attr-defined]
        tax_code=p.tax_code,  # type: ignore[attr-defined]
        address=p.address,  # type: ignore[attr-defined]
        civic_number=p.civic_number,  # type: ignore[attr-defined]
        postal_code=p.postal_code,  # type: ignore[attr-defined]
        city=p.city,  # type: ignore[attr-defined]
        province=p.province,  # type: ignore[attr-defined]
        country=p.country,  # type: ignore[attr-defined]
        sdi_code=p.sdi_code,  # type: ignore[attr-defined]
        pec=p.pec,  # type: ignore[attr-defined]
        invoice_series=p.invoice_series,  # type: ignore[attr-defined]
        payment_iban=p.payment_iban,  # type: ignore[attr-defined]
        description=p.description,  # type: ignore[attr-defined]
        default_billable=p.default_billable,  # type: ignore[attr-defined]
        hourly_rate=p.hourly_rate,  # type: ignore[attr-defined]
        currency=p.currency,  # type: ignore[attr-defined]
        timezone=p.timezone,  # type: ignore[attr-defined]
        default_payment_conditions_code=p.default_payment_conditions_code,  # type: ignore[attr-defined]
        default_payment_method_code=p.default_payment_method_code,  # type: ignore[attr-defined]
        default_payment_terms_days=p.default_payment_terms_days,  # type: ignore[attr-defined]
        invoice_language=p.invoice_language,  # type: ignore[attr-defined]
        invoice_date_format=p.invoice_date_format,  # type: ignore[attr-defined]
    )


def _project_out(t: Tag, p: object) -> ProjectOut:
    return ProjectOut(
        id=t.id,
        name=t.name,
        status=t.status,
        version=t.version,
        client_tag_id=p.client_tag_id,  # type: ignore[attr-defined]
        budget=p.budget,  # type: ignore[attr-defined]
        color=t.color,
        description=p.description,  # type: ignore[attr-defined]
    )


@router.get("/clients", response_model=list[ClientOut])
async def list_clients(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[ClientOut]:
    # A workspace always has the default "Personal" client.
    await taxonomy.ensure_default_client(ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id)
    rows = await taxonomy.list_clients(ctx.session, org_id=ctx.org_id)
    return [_client_out(t, p) for t, p in rows]


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[ProjectOut]:
    rows = await taxonomy.list_projects(ctx.session, org_id=ctx.org_id)
    return [_project_out(t, p) for t, p in rows]


@router.patch("/clients/{tag_id}", status_code=204)
async def patch_client(
    tag_id: uuid.UUID,
    body: ClientPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    ensure_role(ctx.role, Role.owner)
    data = body.model_dump(exclude_unset=True)
    name = data.pop("name", None)
    await taxonomy.update_client(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        tag_id=tag_id,
        name=name,
        fields=data,
    )


@router.patch("/projects/{tag_id}", status_code=204)
async def patch_project(
    tag_id: uuid.UUID,
    body: ProjectPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    ensure_role(ctx.role, Role.owner)
    data = body.model_dump(exclude_unset=True)
    name = data.pop("name", None)
    await taxonomy.update_project(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        tag_id=tag_id,
        name=name,
        fields=data,
    )


# Hard-delete endpoints (`purge`). Two-step destructive op: the client/
# project must already be archived, the workspace default is protected,
# and the service cascades the subgraph (tasks, notes, memory blobs,
# events, attachment objects in the store). Role: owner.
@router.delete("/clients/{tag_id}", status_code=204)
async def delete_client(
    tag_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> None:
    await taxonomy.purge_client(ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, tag_id=tag_id)


@router.delete("/projects/{tag_id}", status_code=204)
async def delete_project(
    tag_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> None:
    await taxonomy.purge_project(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, tag_id=tag_id
    )


@router.patch("/tags/{tag_id}", response_model=VersionOut)
async def patch_tag(
    tag_id: uuid.UUID,
    body: TagPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    version = await taxonomy.update_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        tag_id=tag_id,
        expected_version=body.expected_version,
        name=body.name,
        color=body.color,
        status=body.status,
    )
    return VersionOut(id=tag_id, version=version)
