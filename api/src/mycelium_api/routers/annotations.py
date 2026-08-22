"""Annotations router: inline comments and suggestions on markdown
documents (note-part bodies, task descriptions). Thin adapter over
``mycelium_core.services.annotations`` (docs/adr/0001). The generic
``(doc_kind, doc_id)`` handle is the same on web, CLI and MCP; only the
inline rendering is web-specific."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_api.deps import (
    TenantCtx,
    annotation_body_patch_ctx,
    annotation_body_read_ctx,
    annotation_body_write_ctx,
    current_claims,
    current_claims_optional,
    tenant_ctx,
)
from mycelium_api.schemas import (
    AnnotationAppendIn,
    AnnotationAssignIn,
    AnnotationCommentIn,
    AnnotationEditIn,
    AnnotationOut,
    AnnotationReplaceIn,
    AnnotationUIStateBulkIn,
    AnnotationUIStateIn,
    AppendOut,
    ExpectedVersionIn,
    ReplaceOut,
    RevisionOut,
    RevisionSummaryIn,
    SuggestionIn,
    VersionOut,
)
from mycelium_api.textstream import read_capped_text, read_patch_payload, text_block_headers
from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.annotation import Annotation
from mycelium_core.models.identity import Identity
from mycelium_core.services import annotations as svc
from mycelium_core.services import capability_tokens as capability_tokens_svc
from mycelium_core.services import entity_revisions as rev_svc
from mycelium_core.services import identities as identities_svc

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


async def _author_idents(
    session: AsyncSession, org_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str, str | None]]:
    """Per-identity ``(handle, kind, label)`` for annotation authors, batched
    (mirrors ``tasks._creator_idents``). ``label`` is ``ai_assistants.label``
    when the author is an ai_assistant identity (its user-facing display name);
    ``None`` for a human -- the login handle is in the ``handle`` slot."""
    if not ids:
        return {}
    from mycelium_core.models.ai_assistant import AiAssistant

    rows = (
        await session.execute(
            select(Identity.id, Identity.handle, Identity.kind, AiAssistant.label)
            .outerjoin(AiAssistant, AiAssistant.id == Identity.ai_assistant_id)
            .where(Identity.id.in_(ids), Identity.org_id == org_id)
        )
    ).all()
    return {iid: (handle, kind.value, label) for iid, handle, kind, label in rows}


def annotation_out(
    a: Annotation,
    author: tuple[str, str, str | None] | None = None,
    *,
    ui_collapsed: bool = False,
) -> AnnotationOut:
    """Map a row to the wire shape, collapsing the typed FKs back to the
    generic ``doc_id``. ``author`` is the pre-resolved ``(handle, kind,
    label)`` of ``author_identity_id`` (see ``_author_idents``); when omitted
    the human-name fields stay ``None`` (the raw id is still present).
    ``ui_collapsed`` is the caller's per-user card state (annotation_ui_state);
    the default False = expanded matches "no row"."""
    doc_id = cast(uuid.UUID, a.task_id if a.doc_kind == "task_description" else a.note_part_id)
    handle, kind, label = author if author is not None else (None, None, None)
    return AnnotationOut(
        id=a.id,
        doc_kind=a.doc_kind,
        doc_id=doc_id,
        kind=a.kind,
        body=a.body,
        anchor_quote=a.anchor_quote,
        anchor_prefix=a.anchor_prefix,
        anchor_suffix=a.anchor_suffix,
        anchor_domain=a.anchor_domain,
        original_text=a.original_text,
        proposed_text=a.proposed_text,
        status=a.status,
        parent_id=a.parent_id,
        author_identity_id=a.author_identity_id,
        author_handle=handle,
        author_kind=kind,
        author_label=label,
        resolved_by_identity_id=a.resolved_by_identity_id,
        assigned_to_identity_id=a.assigned_to_identity_id,
        resolved_at=a.resolved_at,
        edited_at=a.edited_at,
        deleted_at=a.deleted_at,
        version=a.version,
        created_at=a.created_at,
        updated_at=a.updated_at,
        ui_collapsed=ui_collapsed,
    )


async def annotations_out(
    session: AsyncSession,
    org_id: uuid.UUID,
    rows: list[Annotation],
    *,
    ui: dict[uuid.UUID, bool] | None = None,
) -> list[AnnotationOut]:
    """Serialise a list of annotations, resolving every author identity to a
    human ``(handle, kind, label)`` in one batched query (no N+1). ``ui`` is
    the caller's ``{annotation_id: collapsed}`` map (``get_ui_states_for_user``);
    ids absent from it — or a ``None`` map — serialise as expanded."""
    idmap = await _author_idents(
        session, org_id, {a.author_identity_id for a in rows if a.author_identity_id}
    )
    return [
        annotation_out(
            a,
            idmap.get(a.author_identity_id) if a.author_identity_id else None,
            ui_collapsed=ui.get(a.id, False) if ui else False,
        )
        for a in rows
    ]


async def annotation_out_one(
    session: AsyncSession,
    org_id: uuid.UUID,
    a: Annotation,
    *,
    ui_collapsed: bool = False,
) -> AnnotationOut:
    """Single-row ``annotations_out`` (resolves the one author)."""
    (only,) = await annotations_out(
        session, org_id, [a], ui={a.id: ui_collapsed} if ui_collapsed else None
    )
    return only


@router.get("", response_model=list[AnnotationOut])
async def list_annotations(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    doc_kind: Annotated[str, Query()],
    doc_id: Annotated[uuid.UUID, Query()],
    include_resolved: Annotated[bool, Query()] = True,
    include_deleted: Annotated[bool, Query()] = False,
) -> list[AnnotationOut]:
    """``include_deleted=True`` surfaces soft-deleted rows, which is how a
    caller finds the id and ``version`` to POST to ``.../restore``."""
    rows = await svc.list_for_doc(
        ctx.session,
        org_id=ctx.org_id,
        doc_kind=doc_kind,
        doc_id=doc_id,
        include_resolved=include_resolved,
        include_deleted=include_deleted,
    )
    ui = await svc.get_ui_states_for_user(
        ctx.session, user_id=ctx.user_id, doc_kind=doc_kind, doc_id=doc_id
    )
    return await annotations_out(ctx.session, ctx.org_id, rows, ui=ui)


# Per-user card collapse state (mirrors the note-part ui-state routes).
# The bulk route is declared before the per-annotation one so the literal
# ``ui-state`` segment is never captured as an ``annotation_id``.
@router.put("/ui-state", response_model=list[AnnotationOut])
async def set_all_annotations_ui_state(
    body: AnnotationUIStateBulkIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[AnnotationOut]:
    """Collapse/expand every card on one document for the caller in a single
    upsert, returning the refreshed list so the SPA syncs from one response."""
    ui = await svc.set_ui_states_bulk(
        ctx.session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        doc_kind=body.doc_kind,
        doc_id=body.doc_id,
        collapsed=body.collapsed,
    )
    rows = await svc.list_for_doc(
        ctx.session, org_id=ctx.org_id, doc_kind=body.doc_kind, doc_id=body.doc_id
    )
    return await annotations_out(ctx.session, ctx.org_id, rows, ui=ui)


@router.put("/{annotation_id}/ui-state", response_model=AnnotationOut)
async def set_annotation_ui_state(
    annotation_id: uuid.UUID,
    body: AnnotationUIStateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AnnotationOut:
    """Persist the caller's collapse state for one card. User-scoped,
    last-write-wins (no version): no row = expanded."""
    await svc.set_ui_state(
        ctx.session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        annotation_id=annotation_id,
        collapsed=body.collapsed,
    )
    a = await svc.get_annotation(ctx.session, org_id=ctx.org_id, annotation_id=annotation_id)
    return await annotation_out_one(ctx.session, ctx.org_id, a, ui_collapsed=body.collapsed)


@router.post("/comment", response_model=AnnotationOut)
async def create_comment(
    body: AnnotationCommentIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> AnnotationOut:
    # Attribute an agent bearer's comment to its ai_assistant identity badge,
    # exactly like /comment/stream and the MCP add_annotation tool. Without
    # this the JSON create records the token-owner's *user* identity, so the
    # author later can't edit/delete via the author gate unless it is admin
    # (transport-invariant authorship: create here => edit/delete anywhere).
    author = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
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
        anchor_domain=body.anchor_domain,
        parent_id=body.parent_id,
        author_identity_id=author,
    )
    return await annotation_out_one(ctx.session, ctx.org_id, a)


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
    return await annotation_out_one(ctx.session, ctx.org_id, a)


@router.post("/suggestion", response_model=AnnotationOut)
async def propose_suggestion(
    body: SuggestionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> AnnotationOut:
    author = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
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
        anchor_domain=body.anchor_domain,
        author_identity_id=author,
    )
    return await annotation_out_one(ctx.session, ctx.org_id, a)


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
    return await annotation_out_one(ctx.session, ctx.org_id, a)


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
        # include_inactive: this inbox exists to answer "what is still
        # assigned to the person we deactivated". Refusing here would
        # hide exactly the backlog someone needs to redistribute.
        ident = await identities_svc.lookup_by_handle(
            ctx.session, org_id=ctx.org_id, handle=handle, include_inactive=True
        )
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
    # The inbox spans documents, so the per-doc ui map doesn't apply;
    # resolve the caller's collapse state by explicit ids instead.
    ui = await svc.get_ui_states_by_ids(
        ctx.session, user_id=ctx.user_id, annotation_ids=[a.id for a in rows]
    )
    return await annotations_out(ctx.session, ctx.org_id, rows, ui=ui)


@router.get("/{annotation_id}", response_model=AnnotationOut)
async def get_annotation(
    annotation_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    include_deleted: Annotated[bool, Query()] = False,
) -> AnnotationOut:
    """``include_deleted=True`` reads a soft-deleted annotation, which is
    how a caller learns the ``version`` that ``.../restore`` requires."""
    a = await svc.get_annotation(
        ctx.session,
        org_id=ctx.org_id,
        annotation_id=annotation_id,
        include_deleted=include_deleted,
    )
    ui = await svc.get_ui_states_by_ids(
        ctx.session, user_id=ctx.user_id, annotation_ids=[annotation_id]
    )
    return await annotation_out_one(
        ctx.session, ctx.org_id, a, ui_collapsed=ui.get(annotation_id, False)
    )


@router.patch("/{annotation_id}", response_model=VersionOut)
async def edit_annotation(
    annotation_id: uuid.UUID,
    body: AnnotationEditIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> VersionOut:
    # Resolve the agent's ai_assistant identity so an agent editing its OWN
    # comment satisfies the author gate on authorship, not on the admin
    # fallback (which a clamped/member effective role does not have). Parity
    # with the /body/stream and /body/patch edit paths.
    ident = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v = await svc.edit(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        body=body.body,
        expected_version=body.expected_version,
        actor_identity_id=ident,
    )
    return VersionOut(id=annotation_id, version=v)


@router.patch("/{annotation_id}/body/stream", response_model=VersionOut)
async def edit_annotation_body_stream(
    annotation_id: uuid.UUID,
    request: Request,
    ctx: Annotated[TenantCtx, Depends(annotation_body_write_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims_optional)],
    expected_version: Annotated[int, Query(ge=1)],
) -> VersionOut:
    """Token-free replace of an annotation's body (a comment's text or a
    suggestion's rationale): the new text is the raw request body,
    streamed in instead of riding a tool argument. ``expected_version``
    is the optimistic cursor (a mismatch is ``stale_version`` -> 409);
    author-or-admin only. Bearer (assistant identity preserved) or a
    single-use ``annotation_body:write`` capability (attributed to the
    token's user), consumed on success. Use the MCP
    ``edit_annotation_body_instructions`` /
    ``set_text_block_capability`` tools for the matching ``curl``."""
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
    if ctx.capability_token_id is not None:
        await capability_tokens_svc.consume(ctx.session, token_id=ctx.capability_token_id)
    return VersionOut(id=annotation_id, version=v)


@router.get("/{annotation_id}/body/raw")
async def download_annotation_body_raw(
    annotation_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(annotation_body_read_ctx, scope="function")],
) -> Response:
    """Token-free raw download of a comment/annotation body. Returns it as
    ``text/markdown`` with ``X-Version`` + ``X-Body-SHA256`` headers (the
    base gate the patch route checks). Bearer or a multi-use
    ``annotation_body:read`` capability for this annotation. Use the MCP
    ``get_text_block_capability`` tool (kind=``annotation``) for the
    matching ``curl -D-``."""
    ann = await svc.get_annotation(ctx.session, org_id=ctx.org_id, annotation_id=annotation_id)
    body = ann.body or ""
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers=text_block_headers(version=ann.version, body=body),
    )


@router.post("/{annotation_id}/body/patch", response_model=VersionOut)
async def patch_annotation_body(
    annotation_id: uuid.UUID,
    request: Request,
    ctx: Annotated[TenantCtx, Depends(annotation_body_patch_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims_optional)],
    expected_version: Annotated[int, Query(ge=1)],
    base_sha256: Annotated[str, Query(min_length=64, max_length=64)],
) -> VersionOut:
    """Apply a strict unified diff (the raw request body) to a
    comment/annotation body. Base gate (``expected_version`` +
    ``base_sha256`` from the ``body/raw`` headers): 409 ``patch.stale`` on
    drift, 422 on a diff that does not apply, nothing mutates on failure.
    Author-or-admin only. Bearer or a single-use ``annotation_body:patch``
    capability, consumed on success. Use the MCP
    ``patch_text_block_capability`` tool (kind=``annotation``)."""
    patch = await read_patch_payload(request)
    ident = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v = await svc.apply_patch_to_body(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=expected_version,
        patch=patch,
        base_sha256=base_sha256,
        actor_identity_id=ident,
    )
    if ctx.capability_token_id is not None:
        await capability_tokens_svc.consume(ctx.session, token_id=ctx.capability_token_id)
    return VersionOut(id=annotation_id, version=v)


@router.post("/{annotation_id}/body/append", response_model=AppendOut)
async def append_to_annotation_body(
    annotation_id: uuid.UUID,
    body: AnnotationAppendIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> AppendOut:
    """Append text to the END of a comment/annotation body without
    reading it first: the annotation twin of the note-part and
    task-description appends. ``expected_version`` omitted appends onto
    the current version. Author-or-admin."""
    ident = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v, n = await svc.append_to_body(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        text=body.text,
        separator=body.separator,
        expected_version=body.expected_version,
        dedupe_if_tail_matches=body.dedupe_if_tail_matches,
        actor_identity_id=ident,
    )
    return AppendOut(id=annotation_id, version=v, appended_chars=n)


@router.post("/{annotation_id}/body/prepend", response_model=AppendOut)
async def prepend_to_annotation_body(
    annotation_id: uuid.UUID,
    body: AnnotationAppendIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> AppendOut:
    """Prepend text to the FRONT of a comment/annotation body: the mirror
    of the append route, same contract. Author-or-admin."""
    ident = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v, n = await svc.prepend_to_body(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        text=body.text,
        separator=body.separator,
        expected_version=body.expected_version,
        dedupe_if_head_matches=body.dedupe_if_tail_matches,
        actor_identity_id=ident,
    )
    return AppendOut(id=annotation_id, version=v, appended_chars=n)


@router.post("/{annotation_id}/body/replace", response_model=ReplaceOut)
async def replace_in_annotation_body(
    annotation_id: uuid.UUID,
    body: AnnotationReplaceIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> ReplaceOut:
    """Anchored find/replace inside ONE comment/annotation body without
    resending it: swap the literal ``find`` for ``replace``. The twin of
    the note-part replace, which the annotation family lacked entirely.
    ``count=0`` replaces every occurrence; a positive ``count`` only the
    first N. Concurrency-safe via ``expected_version``. A no-op (``find``
    absent) returns ``replacements=0`` and leaves the version untouched.
    Author-or-admin, like every other body write."""
    ident = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v, replacements = await svc.replace_in_body(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        find=body.find,
        replace=body.replace,
        expected_version=body.expected_version,
        count=body.count,
        actor_identity_id=ident,
    )
    return ReplaceOut(id=annotation_id, version=v, replacements=replacements)


@router.post("/{annotation_id}/restore", response_model=VersionOut)
async def restore_annotation(
    annotation_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> VersionOut:
    """Undo a delete: the comment (or withdrawn suggestion) is back in
    the diary. Author-or-admin, the same gate as the delete it reverses.
    Without this the soft delete was a one-way door -- the row was
    retained but unreachable on every surface."""
    ident = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v = await svc.restore(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=body.expected_version,
        actor_identity_id=ident,
    )
    return VersionOut(id=annotation_id, version=v)


def _revision_out(rev: Any, seq: int | None = None) -> RevisionOut:
    """Serialize an EntityRevision row (same shape the task and note
    timelines use; ``org_id`` is dropped -- RLS already scoped it)."""
    return RevisionOut(
        id=rev.id,
        entity_kind=rev.entity_kind,
        entity_id=rev.entity_id,
        snapshot=rev.snapshot or {},
        changed_fields=list(rev.changed_fields or []),
        channel=rev.channel,
        actor_id=rev.actor_id,
        actor_kind=rev.actor_kind,
        actor_subject_id=rev.actor_subject_id,
        edit_session_id=rev.edit_session_id,
        version_from=rev.version_from,
        version_to=rev.version_to,
        seq=seq,
        edit_count=rev.edit_count,
        started_at=rev.started_at,
        last_edit_at=rev.last_edit_at,
        sealed_at=rev.sealed_at,
        restored_from=rev.restored_from,
        summary=rev.summary,
    )


@router.get("/{annotation_id}/revisions", response_model=list[RevisionOut])
async def list_annotation_revisions(
    annotation_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RevisionOut]:
    """Timeline of revisions for this comment, most recent first
    (migration 0090). Comments were the last markdown document with no
    history: ``version`` said an entry had changed, nothing said what it
    used to say."""
    await svc.get_annotation(
        ctx.session, org_id=ctx.org_id, annotation_id=annotation_id, include_deleted=True
    )
    rows = await rev_svc.list_revisions(
        ctx.session,
        entity_kind=rev_svc.ENTITY_KIND_ANNOTATION,
        entity_id=annotation_id,
        limit=limit,
    )
    seqs = await rev_svc.revision_sequence(
        ctx.session,
        entity_kind=rev_svc.ENTITY_KIND_ANNOTATION,
        entity_id=annotation_id,
        only_ids=[r.id for r in rows],
    )
    return [_revision_out(r, seq=seqs.get(r.id)) for r in rows]


@router.get("/{annotation_id}/revisions/{rev_id}", response_model=RevisionOut)
async def get_annotation_revision(
    annotation_id: uuid.UUID,
    rev_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> RevisionOut:
    # The snapshot quotes the document the comment hangs on
    # (``anchor_quote`` / ``original_text``), so the read answers to the
    # comment's perimeter -- which, for a note anchor, is the note's.
    await svc.get_annotation(
        ctx.session, org_id=ctx.org_id, annotation_id=annotation_id, include_deleted=True
    )
    rev = await rev_svc.get_revision(
        ctx.session,
        revision_id=rev_id,
        entity_kind=rev_svc.ENTITY_KIND_ANNOTATION,
        entity_id=annotation_id,
    )
    return _revision_out(rev)


@router.patch("/{annotation_id}/revisions/{rev_id}", response_model=RevisionOut)
async def update_annotation_revision_summary(
    annotation_id: uuid.UUID,
    rev_id: uuid.UUID,
    body: RevisionSummaryIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> RevisionOut:
    """Set / clear the ``summary`` label on a revision. Same summary
    contract as the /notes and /tasks endpoints, with one difference: a
    comment's snapshot quotes the document it hangs on, so for a note
    anchor this follows the NOTE's perimeter -- where the note and task
    twins keep the bin readable for their own restore flow, a thread on a
    binned note is unreachable until the note is restored."""
    await svc.get_annotation(
        ctx.session, org_id=ctx.org_id, annotation_id=annotation_id, include_deleted=True
    )
    rev = await rev_svc.set_summary(
        ctx.session,
        revision_id=rev_id,
        summary=body.summary,
        entity_kind=rev_svc.ENTITY_KIND_ANNOTATION,
        entity_id=annotation_id,
    )
    return _revision_out(rev)


@router.post("/{annotation_id}/revisions/{rev_id}/restore", response_model=VersionOut)
async def restore_annotation_revision(
    annotation_id: uuid.UUID,
    rev_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> VersionOut:
    """Revert the comment's BODY to this revision. Only the body is
    restorable: never the thread's status, assignee or anchoring."""
    ident = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v = await svc.restore_revision(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        revision_id=rev_id,
        expected_version=body.expected_version,
        actor_identity_id=ident,
    )
    return VersionOut(id=annotation_id, version=v)


@router.post("/{annotation_id}/purge", status_code=204)
async def purge_annotation(
    annotation_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    """PURGE: destroy the comment, its card state and its entire revision
    history. Irreversible, admin-only, and a separate path from DELETE --
    which is the ordinary restorable removal and stays that way."""
    await svc.purge(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
    )


@router.delete("/{annotation_id}", response_model=VersionOut)
async def delete_annotation(
    annotation_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
    expected_version: Annotated[int, Query(ge=1)],
) -> VersionOut:
    # Same authorship-not-admin resolution as the edit path: an agent must be
    # able to delete/withdraw its OWN comment/suggestion under any effective
    # role.
    ident = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v = await svc.soft_delete(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=expected_version,
        actor_identity_id=ident,
    )
    return VersionOut(id=annotation_id, version=v)


@router.post("/{annotation_id}/resolve", response_model=VersionOut)
async def resolve_annotation(
    annotation_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> VersionOut:
    # Stamp resolved_by with the agent's ai_assistant badge (parity with the
    # MCP resolve tool), not the token-owner's user identity.
    by = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v = await svc.resolve(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=body.expected_version,
        resolved_by_identity_id=by,
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
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> VersionOut:
    by = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v = await svc.accept_suggestion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=body.expected_version,
        resolved_by_identity_id=by,
        # Accepting rewrites the target note/task body, so the service requires
        # write on that family too, not just comments:write (task c19f2f63,
        # enabler B). None (human session / bare token) = full access.
        granted_scopes=claims.get("assistant_scope"),
    )
    return VersionOut(id=annotation_id, version=v)


@router.post("/{annotation_id}/reject", response_model=VersionOut)
async def reject_suggestion(
    annotation_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> VersionOut:
    by = await _author_identity_id(ctx.session, org_id=ctx.org_id, claims=claims)
    v = await svc.reject_suggestion(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        annotation_id=annotation_id,
        expected_version=body.expected_version,
        resolved_by_identity_id=by,
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
