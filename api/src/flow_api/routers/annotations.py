"""Annotations router: inline comments and suggestions on markdown
documents (note-part bodies, task descriptions). Thin adapter over
``flow_core.services.annotations`` (docs/adr/0001). The generic
``(doc_kind, doc_id)`` handle is the same on web, CLI and MCP; only the
inline rendering is web-specific."""

from __future__ import annotations

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query

from flow_api.deps import TenantCtx, tenant_ctx
from flow_api.schemas import (
    AnnotationCommentIn,
    AnnotationEditIn,
    AnnotationOut,
    ExpectedVersionIn,
    SuggestionIn,
    VersionOut,
)
from flow_core.models.annotation import Annotation
from flow_core.services import annotations as svc

router = APIRouter(prefix="/annotations", tags=["annotations"])


def annotation_out(a: Annotation) -> AnnotationOut:
    """Map a row to the wire shape, collapsing the typed FKs back to the
    generic ``doc_id``."""
    doc_id = cast(uuid.UUID, a.task_id if a.doc_kind == "task_description" else a.note_part_id)
    return AnnotationOut(
        id=a.id,
        doc_kind=a.doc_kind,
        doc_id=doc_id,
        kind=a.kind,
        body=a.body,
        anchor_quote=a.anchor_quote,
        anchor_prefix=a.anchor_prefix,
        anchor_suffix=a.anchor_suffix,
        original_text=a.original_text,
        proposed_text=a.proposed_text,
        status=a.status,
        parent_id=a.parent_id,
        author_identity_id=a.author_identity_id,
        resolved_by_identity_id=a.resolved_by_identity_id,
        resolved_at=a.resolved_at,
        edited_at=a.edited_at,
        deleted_at=a.deleted_at,
        version=a.version,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


@router.get("", response_model=list[AnnotationOut])
async def list_annotations(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    doc_kind: Annotated[str, Query()],
    doc_id: Annotated[uuid.UUID, Query()],
    include_resolved: Annotated[bool, Query()] = True,
) -> list[AnnotationOut]:
    rows = await svc.list_for_doc(
        ctx.session,
        org_id=ctx.org_id,
        doc_kind=doc_kind,
        doc_id=doc_id,
        include_resolved=include_resolved,
    )
    return [annotation_out(a) for a in rows]


@router.post("/comment", response_model=AnnotationOut)
async def create_comment(
    body: AnnotationCommentIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AnnotationOut:
    a = await svc.create_comment(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        doc_kind=body.doc_kind,
        doc_id=body.doc_id,
        body=body.body,
        anchor_quote=body.anchor_quote,
        anchor_prefix=body.anchor_prefix,
        anchor_suffix=body.anchor_suffix,
        parent_id=body.parent_id,
    )
    return annotation_out(a)


@router.post("/suggestion", response_model=AnnotationOut)
async def propose_suggestion(
    body: SuggestionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AnnotationOut:
    a = await svc.propose_suggestion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        doc_kind=body.doc_kind,
        doc_id=body.doc_id,
        original_text=body.original_text,
        proposed_text=body.proposed_text,
        rationale=body.rationale,
        anchor_prefix=body.anchor_prefix,
        anchor_suffix=body.anchor_suffix,
    )
    return annotation_out(a)


@router.get("/{annotation_id}", response_model=AnnotationOut)
async def get_annotation(
    annotation_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AnnotationOut:
    a = await svc.get_annotation(ctx.session, org_id=ctx.org_id, annotation_id=annotation_id)
    return annotation_out(a)


@router.patch("/{annotation_id}", response_model=VersionOut)
async def edit_annotation(
    annotation_id: uuid.UUID,
    body: AnnotationEditIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    v = await svc.edit(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        body=body.body,
        expected_version=body.expected_version,
    )
    return VersionOut(id=annotation_id, version=v)


@router.delete("/{annotation_id}", response_model=VersionOut)
async def delete_annotation(
    annotation_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    expected_version: Annotated[int, Query(ge=1)],
) -> VersionOut:
    v = await svc.soft_delete(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=expected_version,
    )
    return VersionOut(id=annotation_id, version=v)


@router.post("/{annotation_id}/resolve", response_model=VersionOut)
async def resolve_annotation(
    annotation_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    v = await svc.resolve(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=annotation_id, version=v)


@router.post("/{annotation_id}/reopen", response_model=VersionOut)
async def reopen_annotation(
    annotation_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    v = await svc.reopen(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=annotation_id, version=v)


@router.post("/{annotation_id}/accept", response_model=VersionOut)
async def accept_suggestion(
    annotation_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    v = await svc.accept_suggestion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=annotation_id, version=v)


@router.post("/{annotation_id}/reject", response_model=VersionOut)
async def reject_suggestion(
    annotation_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    v = await svc.reject_suggestion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=annotation_id, version=v)
