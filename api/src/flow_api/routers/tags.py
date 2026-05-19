"""Tags / clients / projects router. Thin adapter over the service
layer (docs/adr/0001, 0003)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
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
from flow_core.models.membership import Role
from flow_core.models.tag import Tag, TagKind
from flow_core.services import taxonomy
from flow_core.services.rbac import ensure_role
from flow_core.services.taxonomy import ClientInput

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
async def create_tag(body: TagCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]) -> TagOut:
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
    kind: TagKind | None = None,
    for_project: uuid.UUID | None = None,
) -> list[TagOut]:
    tags = await taxonomy.list_tags(
        ctx.session, org_id=ctx.org_id, kind=kind, for_project=for_project
    )
    scopes = await taxonomy.scopes_by_tag(ctx.session, tag_ids=[t.id for t in tags])
    return [_out(t, scopes.get(t.id, [])) for t in tags]


@router.put("/tags/{tag_id}/scope", status_code=204)
async def set_tag_scope(
    tag_id: uuid.UUID,
    body: TagScopeIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    ensure_role(ctx.role, Role.admin)
    await taxonomy.set_tag_scope(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        tag_id=tag_id,
        target_ids=body.target_ids,
    )


@router.post("/clients", response_model=TagOut)
async def create_client(
    body: ClientCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]
) -> TagOut:
    ensure_role(ctx.role, Role.admin)
    profile = ClientInput(
        ragione_sociale=body.ragione_sociale,
        id_paese=body.id_paese,
        id_codice=body.id_codice,
        codice_fiscale=body.codice_fiscale,
        indirizzo=body.indirizzo,
        cap=body.cap,
        comune=body.comune,
        provincia=body.provincia,
        nazione=body.nazione,
        codice_destinatario=body.codice_destinatario,
        pec=body.pec,
        description=body.description,
        default_billable=body.default_billable,
        tariffa=body.tariffa,
        valuta=body.valuta,
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
    body: ProjectCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]
) -> TagOut:
    ensure_role(ctx.role, Role.admin)
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
        ragione_sociale=p.ragione_sociale,  # type: ignore[attr-defined]
        id_paese=p.id_paese,  # type: ignore[attr-defined]
        id_codice=p.id_codice,  # type: ignore[attr-defined]
        codice_fiscale=p.codice_fiscale,  # type: ignore[attr-defined]
        indirizzo=p.indirizzo,  # type: ignore[attr-defined]
        cap=p.cap,  # type: ignore[attr-defined]
        comune=p.comune,  # type: ignore[attr-defined]
        provincia=p.provincia,  # type: ignore[attr-defined]
        nazione=p.nazione,  # type: ignore[attr-defined]
        codice_destinatario=p.codice_destinatario,  # type: ignore[attr-defined]
        pec=p.pec,  # type: ignore[attr-defined]
        description=p.description,  # type: ignore[attr-defined]
        default_billable=p.default_billable,  # type: ignore[attr-defined]
        tariffa=p.tariffa,  # type: ignore[attr-defined]
        valuta=p.valuta,  # type: ignore[attr-defined]
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[ClientOut]:
    # A workspace always has the default "Personal" client.
    await taxonomy.ensure_default_client(ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id)
    rows = await taxonomy.list_clients(ctx.session, org_id=ctx.org_id)
    return [_client_out(t, p) for t, p in rows]


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> list[ProjectOut]:
    rows = await taxonomy.list_projects(ctx.session, org_id=ctx.org_id)
    return [_project_out(t, p) for t, p in rows]


@router.patch("/clients/{tag_id}", status_code=204)
async def patch_client(
    tag_id: uuid.UUID,
    body: ClientPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    ensure_role(ctx.role, Role.admin)
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
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
) -> None:
    ensure_role(ctx.role, Role.admin)
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


@router.patch("/tags/{tag_id}", response_model=VersionOut)
async def patch_tag(
    tag_id: uuid.UUID,
    body: TagPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx)],
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
