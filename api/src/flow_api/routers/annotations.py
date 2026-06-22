"""Annotations router: inline comments and suggestions on markdown
documents (note-part bodies, task descriptions). Thin adapter over
``flow_core.services.annotations`` (docs/adr/0001). The generic
``(doc_kind, doc_id)`` handle is the same on web, CLI and MCP; only the
inline rendering is web-specific."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_api.deps import TenantCtx, current_claims, tenant_ctx
from flow_api.schemas import (
    AnnotationAssignIn,
    AnnotationCommentIn,
    AnnotationEditIn,
    AnnotationOut,
    ExpectedVersionIn,
    SuggestionIn,
    VersionOut,
)
from flow_api.textstream import read_capped_text
from flow_core.config import get_settings
from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.annotation import Annotation
from flow_core.models.identity import Identity
from flow_core.services import annotations as svc
from flow_core.services import identities as identities_svc

router = APIRouter(prefix="/annotations", tags=["annotations"])


async def _author_identity_id(
    session: AsyncSession, *, org_id: uuid.UUID, claims: dict[str, Any]
) -> uuid.UUID | None:
    """The AI-assistant identity behind an agent token, so an agent's
    token-free streaming write is attributed to the same identity badge
    as its MCP-tool writes (``_resolve_agent_context``). ``None`` for a
    human bearer or a bare token, in which case the service defaults to
    the actor's user identity."""
    if claims.get("typ") != "agent":
        return None
    assistant_id = claims.get("assistant_id")
    if not assistant_id:
        return None
    row = await session.execute(
        select(Identity.id).where(
            Identity.ai_assistant_id == uuid.UUID(assistant_id),
            Identity.org_id == org_id,
        )
    )
    return row.scalar_one_or_none()


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
        assigned_to_identity_id=a.assigned_to_identity_id,
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


@router.post("/comment/stream", response_model=AnnotationOut)
async def create_comment_stream(
    request: Request,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
    doc_kind: Annotated[str, Query()],
    doc_id: Annotated[uuid.UUID, Query()],
    anchor_quote: Annotated[str | None, Query(max_length=8192)] = None,
    anchor_prefix: Annotated[str | None, Query(max_length=2048)] = None,
    anchor_suffix: Annotated[str | None, Query(max_length=2048)] = None,
    parent_id: Annotated[uuid.UUID | None, Query()] = None,
) -> AnnotationOut:
    """Token-free comment: the comment text is the raw request body,
    streamed into ``annotation.body`` instead of riding a tool argument
    (the inline-body analogue of ``POST /attachments/stream``; no S3).
    The bounded anchor fields stay query params (the agent already holds
    them). Body is size-capped + UTF-8; an empty body is rejected. An
    agent token attributes the comment to its AI-assistant identity, same
    as the MCP tool. Use the MCP ``add_comment_instructions`` tool for
    the matching ``curl``."""
    body_text = await read_capped_text(request, max_bytes=get_settings().note_body_max_bytes)
    if not body_text:
        raise DomainError(MessageCode.ANNOTATION_BODY_REQUIRED)
    author = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    a = await svc.create_comment(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        doc_kind=doc_kind,
        doc_id=doc_id,
        body=body_text,
        anchor_quote=anchor_quote,
        anchor_prefix=anchor_prefix,
        anchor_suffix=anchor_suffix,
        parent_id=parent_id,
        author_identity_id=author,
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


@router.post("/suggestion/stream", response_model=AnnotationOut)
async def propose_suggestion_stream(
    request: Request,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
    doc_kind: Annotated[str, Query()],
    doc_id: Annotated[uuid.UUID, Query()],
    original_text: Annotated[str, Query(min_length=1, max_length=8192)],
    rationale: Annotated[str, Query(max_length=4096)] = "",
    anchor_prefix: Annotated[str | None, Query(max_length=2048)] = None,
    anchor_suffix: Annotated[str | None, Query(max_length=2048)] = None,
) -> AnnotationOut:
    """Token-free suggestion: the PROPOSED replacement (the large
    free-form field) is the raw request body; the struck ``original_text``
    it replaces is a query param (a bounded anchor the agent already
    holds, capped to keep the URL short). An empty body is a deletion
    suggestion. Nothing touches the document until the suggestion is
    accepted. Use the MCP ``propose_suggestion_instructions`` tool for the
    matching ``curl``."""
    proposed_text = await read_capped_text(request, max_bytes=get_settings().note_body_max_bytes)
    author = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    a = await svc.propose_suggestion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        doc_kind=doc_kind,
        doc_id=doc_id,
        original_text=original_text,
        proposed_text=proposed_text,
        rationale=rationale,
        anchor_prefix=anchor_prefix,
        anchor_suffix=anchor_suffix,
        author_identity_id=author,
    )
    return annotation_out(a)


@router.get("/assigned", response_model=list[AnnotationOut])
async def list_assigned_annotations(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    handle: Annotated[str | None, Query()] = None,
    include_resolved: Annotated[bool, Query()] = False,
) -> list[AnnotationOut]:
    """The "assigned to me" inbox: annotations assigned to ``handle``
    (defaults to the calling user's identity), newest first. Open-only unless
    ``include_resolved=true``. An unknown handle yields an empty list.
    Declared before ``/{annotation_id}`` so the static path wins."""
    if handle:
        ident = await identities_svc.lookup_by_handle(ctx.session, org_id=ctx.org_id, handle=handle)
        if ident is None:
            return []
        ident_id = ident.id
    else:
        ident_id = (
            await identities_svc.ensure_for_user(
                ctx.session, org_id=ctx.org_id, user_id=ctx.user_id
            )
        ).id
    rows = await svc.list_assigned(
        ctx.session,
        org_id=ctx.org_id,
        assignee_identity_id=ident_id,
        include_resolved=include_resolved,
    )
    return [annotation_out(a) for a in rows]


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


@router.patch("/{annotation_id}/body/stream", response_model=VersionOut)
async def edit_annotation_body_stream(
    annotation_id: uuid.UUID,
    request: Request,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
    expected_version: Annotated[int, Query(ge=1)],
) -> VersionOut:
    """Token-free replace of an annotation's body (a comment's text or a
    suggestion's rationale): the new text is the raw request body,
    streamed in instead of riding a tool argument. ``expected_version``
    is the optimistic cursor (a mismatch is ``stale_version`` -> 409);
    author-or-admin only. Use the MCP ``edit_annotation_body_instructions``
    tool for the matching ``curl``."""
    body_text = await read_capped_text(request, max_bytes=get_settings().note_body_max_bytes)
    if not body_text:
        raise DomainError(MessageCode.ANNOTATION_BODY_REQUIRED)
    ident = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v = await svc.edit(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        body=body_text,
        expected_version=expected_version,
        actor_identity_id=ident,
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


@router.post("/{annotation_id}/assign", response_model=VersionOut)
async def assign_annotation(
    annotation_id: uuid.UUID,
    body: AnnotationAssignIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    """Assign an annotation to a workspace identity (by id or handle), or
    clear it (``clear=true``). Any member may assign -- it is coordination,
    not authorship."""
    v = await svc.assign(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=body.expected_version,
        assignee_identity_id=body.assignee_identity_id,
        assignee_handle=body.assignee_handle,
        clear=body.clear,
    )
    return VersionOut(id=annotation_id, version=v)
