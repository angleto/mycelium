"""Tags / clients / projects router. Thin adapter over the service
layer (docs/adr/0001, 0003)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    ClientCreateIn,
    ProjectCreateIn,
    TagCreateIn,
    TagOut,
    TagPatchIn,
    VersionOut,
)
from flow_core.models.tag import Tag, TagKind
from flow_core.services import taxonomy
from flow_core.services.taxonomy import ClientInput

router = APIRouter(tags=["taxonomy"])


def _out(tag: Tag) -> TagOut:
    return TagOut(
        id=tag.id,
        kind=tag.kind,
        name=tag.name,
        color=tag.color,
        status=tag.status,
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
) -> list[TagOut]:
    tags = await taxonomy.list_tags(ctx.session, org_id=ctx.org_id, kind=kind)
    return [_out(t) for t in tags]


@router.post("/clients", response_model=TagOut)
async def create_client(
    body: ClientCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx)]
) -> TagOut:
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
    tag = await taxonomy.create_project(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        name=body.name,
        client_tag_id=body.client_tag_id,
        tariffa=body.tariffa,
        valuta=body.valuta,
        budget=body.budget,
    )
    return _out(tag)


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
