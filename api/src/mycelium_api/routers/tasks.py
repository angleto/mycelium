"""Tasks router. Thin adapter over the service layer (docs/adr/0001)."""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import select

from mycelium_api.deps import (
    TenantCtx,
    current_claims,
    require_agent_scope,
    task_attachment_write_ctx,
    task_description_patch_ctx,
    task_description_read_ctx,
    task_description_write_ctx,
    tenant_ctx,
)
from mycelium_api.routers.annotations import annotation_out_one, annotations_out
from mycelium_api.routers.attachments import att_out, read_capped, upload_file_field
from mycelium_api.schemas import (
    AnnotationOut,
    AppendOut,
    AssigneeIn,
    AttachmentOut,
    CommentCreateIn,
    EditSessionSealIn,
    EditSessionSealOut,
    ExpectedVersionIn,
    HandoffOut,
    NoteOut,
    NoteTaskLinkOut,
    ParticipantIn,
    ParticipantOut,
    ReminderIn,
    ReminderOut,
    RevisionOut,
    RevisionRestoreIn,
    RevisionSummaryIn,
    StateOut,
    TagBrief,
    TagRefIn,
    TaskChecklistClearDoneOut,
    TaskChecklistItemCreateIn,
    TaskChecklistItemOut,
    TaskChecklistItemPatchIn,
    TaskChecklistReorderIn,
    TaskCreateIn,
    TaskDescriptionAppendIn,
    TaskDescriptionPrependIn,
    TaskNoteCreateIn,
    TaskNoteLinkIn,
    TaskNoteLinksOut,
    TaskOut,
    TaskPatchIn,
    TaskStateIn,
    VersionOut,
)
from mycelium_api.textstream import read_capped_text, read_patch_payload, text_block_headers
from mycelium_core.config import get_settings
from mycelium_core.models.identity import IdentityKind
from mycelium_core.models.note import Note
from mycelium_core.models.tag import Tag
from mycelium_core.models.task import Task
from mycelium_core.models.task_checklist_item import TaskChecklistItem
from mycelium_core.models.task_handoff import TaskHandoff
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import attachments as att_svc
from mycelium_core.services import capability_tokens as capability_tokens_svc
from mycelium_core.services import coordination as coord_svc
from mycelium_core.services import entity_revisions as rev_svc
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import notifications as notif_svc
from mycelium_core.services import participants as part_svc
from mycelium_core.services import task_checklist as checklist_svc
from mycelium_core.services import tasks as svc
from mycelium_core.services import workflow as wf
from mycelium_core.timewindow import split_due

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _parse_due(raw: str | None) -> datetime.date | datetime.datetime | None:
    """Parse the ``due_date`` input into a date (date-only intent) or an
    aware datetime; the service promotes the date-only case to end-of-day
    in the owner's timezone. A malformed value is a 422, not a 500."""
    if raw is None:
        return None
    try:
        return split_due(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid due_date: {raw!r}",
        ) from exc


def _checklist_item_out(it: TaskChecklistItem) -> TaskChecklistItemOut:
    return TaskChecklistItemOut(
        id=it.id,
        task_id=it.task_id,
        note_id=it.note_id,
        text=it.text,
        body=it.body,
        done=it.done,
        position=it.position,
        done_at=it.done_at,
        done_by=it.done_by,
        created_by=it.created_by,
        created_at=it.created_at,
        updated_at=it.updated_at,
        version=it.version,
    )


def _out(
    t: Task,
    state_name: str,
    tags: list[Tag] | None = None,
    assignee_handle: str | None = None,
    assignee_kind: str | None = None,
    created_by_handle: str | None = None,
    created_by_kind: str | None = None,
    created_by_label: str | None = None,
    checklist: list[TaskChecklistItem] | None = None,
    include_description: bool = True,
) -> TaskOut:
    from mycelium_core.models.task import ExecKind

    # docs/adr/0029 P2: derive executor_kind for SPA backward compat.
    # If the assignee is an ai_assistant identity (looked up by the
    # caller), kind is llm_agent; otherwise the task's fallback
    # ``executor_kind`` hint is authoritative (ADR-0028).
    eff_kind = (
        ExecKind.llm_agent
        if assignee_kind == "ai_assistant"
        else (ExecKind.human if assignee_kind == "user" else t.executor_kind)
    )
    return TaskOut(
        tags=[TagBrief(id=g.id, kind=g.kind, name=g.name, color=g.color) for g in (tags or [])],
        id=t.id,
        title=t.title,
        description=t.description if include_description else None,
        state_id=t.state_id,
        state=state_name,
        priority=t.priority,
        importance=t.importance,
        urgency=t.urgency,
        start_date=t.start_date,
        due_date=t.due_date,
        parent_task_id=t.parent_task_id,
        assignee_id=t.assignee_id,
        assignee_handle=assignee_handle,
        assignee_kind=assignee_kind,
        created_by_identity_id=t.created_by_identity_id,
        created_by_handle=created_by_handle,
        created_by_kind=created_by_kind,
        created_by_label=created_by_label,
        owner_id=t.owner_id,
        executor_kind=eff_kind,
        estimate_effort_h=t.estimate_effort_h,
        required_capabilities=list(t.required_capabilities or []),
        monetary_cost=t.monetary_cost,
        location=t.location,
        necessity=t.necessity,
        budget_id=t.budget_id,
        billable=t.billable,
        is_archived=t.is_archived,
        offered=t.offered,
        deleted_at=t.deleted_at,
        created_at=t.created_at,
        updated_at=t.updated_at,
        version=t.version,
        start_at=t.start_at,
        duration_minutes=t.duration_minutes,
        recurrence=t.recurrence,
        checklist=[_checklist_item_out(it) for it in (checklist or [])],
    )


def _note_out(
    n: Note,
    tags: list[Tag] | None = None,
    primary_task_id: uuid.UUID | None = None,
    task_title: str | None = None,
    transcript: str | None = None,
) -> NoteOut:
    # Built exactly like routers/notes.py::_out so the SPA gets the same
    # NoteOut shape (incl. tags + task_id) from either entry point.
    # docs/adr/0029 P3: task_id is derived from the typed link table.
    # Phase 6 final: ``transcript`` is derived from note_part(ord=0)+
    # at the caller, not from a Note column.
    # Migration 0016: ``project_id`` is derived from the project-kind
    # tag in ``tags`` (junction is the source of truth).
    project_id = next(
        (t.id for t in (tags or []) if getattr(t.kind, "value", t.kind) == "project"),
        None,
    )
    return NoteOut(
        id=n.id,
        project_id=project_id,
        task_id=primary_task_id,
        task_title=task_title,
        kind=n.kind,
        status=n.status,
        title=n.title,
        transcript=transcript,
        summary=n.summary,
        audio_ref=n.audio_ref,
        is_archived=n.is_archived,
        deleted_at=n.deleted_at,
        tags=[TagBrief(id=t.id, kind=t.kind, name=t.name, color=t.color) for t in (tags or [])],
        version=n.version,
    )


async def _assignee_idents(
    ctx: TenantCtx, task_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    """docs/adr/0028: per-task (handle, kind) of the assignee identity,
    denormalised for the SPA's task card and the IdentityBadge in the
    list rows (Punto 4). Batch-loaded so a list endpoint pays one
    query, not one per task. Returns an empty mapping for unassigned
    tasks; callers default to ``None`` / fallback in the serializer."""
    if not task_ids:
        return {}
    from mycelium_core.models.identity import Identity

    rows = (
        await ctx.session.execute(
            select(Task.id, Identity.handle, Identity.kind)
            .join(Identity, Identity.id == Task.assignee_id)
            .where(Task.id.in_(task_ids))
        )
    ).all()
    return {tid: (handle, kind.value) for tid, handle, kind in rows}


async def _creator_idents(
    ctx: TenantCtx, task_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str, str | None]]:
    """Per-task ``(handle, kind, label)`` of the AI assistant identity
    behind ``created_by_identity_id``. ``label`` is
    ``ai_assistants.label`` (the user-facing display name) when the
    identity is an ai_assistant; ``None`` otherwise — the user-side
    handle is in the ``handle`` slot. Migrations 0091/0093."""
    if not task_ids:
        return {}
    from mycelium_core.models.ai_assistant import AiAssistant
    from mycelium_core.models.identity import Identity

    rows = (
        await ctx.session.execute(
            select(Task.id, Identity.handle, Identity.kind, AiAssistant.label)
            .join(Identity, Identity.id == Task.created_by_identity_id)
            .outerjoin(AiAssistant, AiAssistant.id == Identity.ai_assistant_id)
            .where(Task.id.in_(task_ids))
        )
    ).all()
    return {tid: (handle, kind.value, label) for tid, handle, kind, label in rows}


async def _creator_tokens(ctx: TenantCtx, task_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Per-task ``agent_tokens.name`` for tasks that recorded a token
    id but no identity (bare MCP tokens — migration 0093). Lets the
    SPA badge AI authorship even before the legacy token is upgraded
    to the ai_assistants flow."""
    if not task_ids:
        return {}
    from mycelium_core.models.agent_token import AgentToken

    rows = (
        await ctx.session.execute(
            select(Task.id, AgentToken.name)
            .join(AgentToken, AgentToken.id == Task.created_by_token_id)
            .where(Task.id.in_(task_ids))
        )
    ).all()
    return {tid: name for tid, name in rows}


def _resolve_creator(
    task: Task,
    idents: dict[uuid.UUID, tuple[str, str, str | None]],
    tokens: dict[uuid.UUID, str],
) -> tuple[str | None, str | None, str | None]:
    """Collapse the identity + token lookups into the three serializer
    slots ``(handle, kind, label)``. Precedence: an ai_assistant
    identity wins (kind=ai_assistant, label=ai_assistants.label,
    handle=identities.handle); else a bare token marks the task as
    AI authored too (kind=mcp_token, label=agent_tokens.name); else
    the user identity (kind=user, handle=identities.handle)."""
    ident = idents.get(task.id)
    if ident is not None:
        handle, kind, ai_label = ident
        if kind == "ai_assistant":
            return handle, kind, ai_label or handle
        return handle, kind, None
    token_name = tokens.get(task.id)
    if token_name is not None:
        return None, "mcp_token", token_name
    return None, None, None


async def _state_names(ctx: TenantCtx, state_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not state_ids:
        return {}
    rows = (
        await ctx.session.execute(
            select(WorkflowState.id, WorkflowState.name).where(WorkflowState.id.in_(state_ids))
        )
    ).all()
    return {sid: name for sid, name in rows}


@router.post("", response_model=TaskOut)
async def create_task(
    body: TaskCreateIn, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> TaskOut:
    task = await svc.create_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        title=body.title,
        description=body.description,
        importance=body.importance,
        urgency=body.urgency,
        start_date=body.start_date,
        due_date=_parse_due(body.due_date),
        billable=body.billable,
        parent_task_id=body.parent_task_id,
        executor_kind=body.executor_kind,
        assignee_id=body.assignee_id,
        assignee_handle=body.assignee_handle,
        owner_id=body.owner_id,
        estimate_effort_h=body.estimate_effort_h,
        required_capabilities=body.required_capabilities,
        monetary_cost=body.monetary_cost,
        location=body.location,
        necessity=body.necessity,
        budget_id=body.budget_id,
        tag_ids=body.tag_ids,
        assignee_ids=body.assignee_ids,
        start_at=body.start_at,
        duration_minutes=body.duration_minutes,
        recurrence=body.recurrence,
    )
    names = await _state_names(ctx, {task.state_id})
    tagmap = await svc.tags_by_task(ctx.session, task_ids=[task.id])
    idents = await _assignee_idents(ctx, {task.id})
    creators = await _creator_idents(ctx, {task.id})
    ctokens = await _creator_tokens(ctx, {task.id})
    h, k = idents.get(task.id, (None, None))
    ch, ck, cl = _resolve_creator(task, creators, ctokens)
    return _out(
        task,
        names.get(task.state_id, ""),
        tagmap.get(task.id, []),
        assignee_handle=h,
        assignee_kind=k,
        created_by_handle=ch,
        created_by_kind=ck,
        created_by_label=cl,
    )


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    state_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    # docs/adr/0028 Punto 4: identity-axis facets. ``assignee_kind``
    # narrows the polymorphism (user/ai_assistant); ``assignee_handles``
    # is multi-select on the assignee identity handle; ``owner_handles``
    # is multi-select on the owner user handle.
    assignee_kind: IdentityKind | None = None,
    assignee_handles: Annotated[list[str] | None, Query()] = None,
    owner_handles: Annotated[list[str] | None, Query()] = None,
    parent_task_id: uuid.UUID | None = None,
    include_archived: bool = False,
    include_deleted: bool = False,
    # Opt-in checklist embedding for the list endpoint. Off by default
    # (callers that don't search/render the checklist don't pay the
    # extra batch query). The SPA's TasksRoute turns it on so the
    # free-text filter can match item text alongside title / tags /
    # description.
    include_checklist: bool = False,
) -> list[TaskOut]:
    rows = await svc.list_tasks(
        ctx.session,
        org_id=ctx.org_id,
        state_id=state_id,
        tag_id=tag_id,
        assignee_id=assignee_id,
        assignee_kind=assignee_kind,
        assignee_handles=assignee_handles,
        owner_handles=owner_handles,
        parent_task_id=parent_task_id,
        include_archived=include_archived,
        include_deleted=include_deleted,
        with_description=False,
    )
    names = await _state_names(ctx, {t.state_id for t in rows})
    tagmap = await svc.tags_by_task(ctx.session, task_ids=[t.id for t in rows])
    ids = {t.id for t in rows}
    idents = await _assignee_idents(ctx, ids)
    creators = await _creator_idents(ctx, ids)
    ctokens = await _creator_tokens(ctx, ids)
    items_map = (
        await checklist_svc.items_by_task(ctx.session, task_ids=list(ids))
        if include_checklist
        else {}
    )
    out: list[TaskOut] = []
    for t in rows:
        h, k = idents.get(t.id, (None, None))
        ch, ck, cl = _resolve_creator(t, creators, ctokens)
        out.append(
            _out(
                t,
                names.get(t.state_id, ""),
                tagmap.get(t.id, []),
                assignee_handle=h,
                assignee_kind=k,
                created_by_handle=ch,
                created_by_kind=ck,
                created_by_label=cl,
                checklist=items_map.get(t.id, []) if include_checklist else None,
                include_description=False,
            )
        )
    return out


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> TaskOut:
    # include_deleted: a trashed/archived task must still open (read its
    # detail before restoring it from the Trash view). TaskOut carries
    # is_archived/deleted_at so the UI can render it read-only.
    task = await svc.get_task(ctx.session, org_id=ctx.org_id, task_id=task_id, include_deleted=True)
    names = await _state_names(ctx, {task.state_id})
    tagmap = await svc.tags_by_task(ctx.session, task_ids=[task.id])
    idents = await _assignee_idents(ctx, {task.id})
    creators = await _creator_idents(ctx, {task.id})
    ctokens = await _creator_tokens(ctx, {task.id})
    items_map = await checklist_svc.items_by_task(ctx.session, task_ids=[task.id])
    h, k = idents.get(task.id, (None, None))
    ch, ck, cl = _resolve_creator(task, creators, ctokens)
    return _out(
        task,
        names.get(task.state_id, ""),
        tagmap.get(task.id, []),
        assignee_handle=h,
        assignee_kind=k,
        created_by_handle=ch,
        created_by_kind=ck,
        created_by_label=cl,
        checklist=items_map.get(task.id, []),
    )


@router.get("/{task_id}/states", response_model=list[StateOut])
async def task_states(
    task_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> list[StateOut]:
    await svc.get_task(ctx.session, org_id=ctx.org_id, task_id=task_id, include_deleted=True)
    workflow = await wf.effective_workflow_for_task(ctx.session, ctx.org_id, task_id)
    states = await wf.get_states(ctx.session, workflow.id)
    return [
        StateOut(
            id=s.id,
            name=s.name,
            ord=s.ord,
            is_initial=s.is_initial,
            is_terminal=s.is_terminal,
            is_hidden=s.is_hidden,
        )
        for s in states
    ]


@router.patch("/{task_id}", response_model=TaskOut)
async def patch_task(
    task_id: uuid.UUID,
    body: TaskPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    edit_session_id: Annotated[str | None, Header(alias="X-Edit-Session-Id")] = None,
) -> TaskOut:
    # Returns the full canonical TaskOut (not just {id, version}) so any
    # caller (SPA, CLI, MCP, nvim) sees server-derived fields without an
    # extra GET. priority is the motivating one: it is recomputed from
    # importance x urgency by the service whenever both axes are present,
    # and there must be a single source of truth across surfaces (we used
    # to also derive it in JS in TaskDetailRoute, which lied for tasks
    # that had NULL importance/urgency and showed a different priority
    # than the list/kanban — bug fixed by deleting the JS derive).
    values: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if "due_date" in values:
        # The input is a string (bare date or full ISO); the service
        # promotes a date-only value to end-of-day in the owner's tz.
        values["due_date"] = _parse_due(values["due_date"])
    # ``X-Edit-Session-Id`` flips the recovery-history channel to ``web``
    # so consecutive autosaves under the same session coalesce into one
    # open revision. Without the header, every PATCH is a sealed
    # revision (matches MCP / external-API semantics).
    channel = "web" if edit_session_id else "api"
    await svc.update_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
        values=values,
        channel=channel,
        edit_session_id=edit_session_id,
    )
    task = await svc.get_task(ctx.session, org_id=ctx.org_id, task_id=task_id, include_deleted=True)
    names = await _state_names(ctx, {task.state_id})
    tagmap = await svc.tags_by_task(ctx.session, task_ids=[task.id])
    idents = await _assignee_idents(ctx, {task.id})
    creators = await _creator_idents(ctx, {task.id})
    ctokens = await _creator_tokens(ctx, {task.id})
    items_map = await checklist_svc.items_by_task(ctx.session, task_ids=[task.id])
    h, k = idents.get(task.id, (None, None))
    ch, ck, cl = _resolve_creator(task, creators, ctokens)
    return _out(
        task,
        names.get(task.state_id, ""),
        tagmap.get(task.id, []),
        assignee_handle=h,
        assignee_kind=k,
        created_by_handle=ch,
        created_by_kind=ck,
        created_by_label=cl,
        checklist=items_map.get(task.id, []),
    )


@router.post("/{task_id}/description/append", response_model=AppendOut)
async def append_description(
    task_id: uuid.UUID,
    body: TaskDescriptionAppendIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AppendOut:
    """Task 4ac39ecf: context-blind append for ``task.description``
    (mirror of /notes/{id}/append). Lets an MCP / LLM caller add a
    paragraph -- a status note, a follow-up checklist, a finding --
    without re-sending the existing body. ``expected_version`` is
    optional; when omitted the helper appends onto whatever state the
    row currently has."""
    new_version, appended = await svc.append_to_description(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        text=body.text,
        separator=body.separator,
        expected_version=body.expected_version,
        dedupe_if_tail_matches=body.dedupe_if_tail_matches,
    )
    return AppendOut(id=task_id, version=new_version, appended_chars=appended)


@router.post("/{task_id}/description/prepend", response_model=AppendOut)
async def prepend_description(
    task_id: uuid.UUID,
    body: TaskDescriptionPrependIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AppendOut:
    """Task 5662a07f: context-blind prepend for ``task.description``
    (mirror of /description/append). Adds text to the FRONT without
    re-sending the body. ``appended_chars`` reports the prepended count."""
    new_version, prepended = await svc.prepend_to_description(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        text=body.text,
        separator=body.separator,
        expected_version=body.expected_version,
        dedupe_if_head_matches=body.dedupe_if_head_matches,
    )
    return AppendOut(id=task_id, version=new_version, appended_chars=prepended)


@router.get("/{task_id}/description/raw")
async def download_task_description_raw(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(task_description_read_ctx, scope="function")],
) -> Response:
    """Token-free raw download of a task's ``description`` markdown. Returns
    it as ``text/markdown`` with ``X-Version`` + ``X-Body-SHA256`` headers
    (the base gate the patch route checks). Bearer or a multi-use
    ``task_description:read`` capability for this task. Use the MCP
    ``get_text_block_capability`` tool (kind=``task_description``) for the
    matching ``curl -D-``."""
    task = await svc.get_task(ctx.session, org_id=ctx.org_id, task_id=task_id)
    body = task.description or ""
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers=text_block_headers(version=task.version, body=body),
    )


@router.put("/{task_id}/description/stream", response_model=VersionOut)
async def replace_task_description_stream(
    task_id: uuid.UUID,
    request: Request,
    ctx: Annotated[TenantCtx, Depends(task_description_write_ctx, scope="function")],
    expected_version: Annotated[int, Query(ge=1)],
    edit_session_id: Annotated[str | None, Header(alias="X-Edit-Session-Id")] = None,
) -> VersionOut:
    """Token-free full-body replace of a task's ``description``: the new
    markdown is the raw request body, size-capped (``note_body_max_bytes``)
    and UTF-8. ``expected_version`` is the optimistic cursor (mismatch ->
    409). An empty body clears the description. For incremental growth use
    ``/description/append``. Bearer or a single-use
    ``task_description:write`` capability, consumed on success. Use the MCP
    ``set_text_block_capability`` tool (kind=``task_description``)."""
    body_text = await read_capped_text(request, max_bytes=get_settings().note_body_max_bytes)
    channel = "web" if edit_session_id else "api"
    v = await svc.update_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=expected_version,
        values={"description": body_text},
        channel=channel,
        edit_session_id=edit_session_id,
    )
    if ctx.capability_token_id is not None:
        await capability_tokens_svc.consume(ctx.session, token_id=ctx.capability_token_id)
    return VersionOut(id=task_id, version=v)


@router.post("/{task_id}/description/patch", response_model=VersionOut)
async def patch_task_description(
    task_id: uuid.UUID,
    request: Request,
    ctx: Annotated[TenantCtx, Depends(task_description_patch_ctx, scope="function")],
    expected_version: Annotated[int, Query(ge=1)],
    base_sha256: Annotated[str, Query(min_length=64, max_length=64)],
    edit_session_id: Annotated[str | None, Header(alias="X-Edit-Session-Id")] = None,
) -> VersionOut:
    """Apply a strict unified diff (the raw request body) to a task's
    ``description``. Base gate (``expected_version`` + ``base_sha256`` from
    the ``description/raw`` headers): 409 ``patch.stale`` on drift, 422 on a
    diff that does not apply, nothing mutates on failure. Bearer or a
    single-use ``task_description:patch`` capability, consumed on success.
    Use the MCP ``patch_text_block_capability`` tool
    (kind=``task_description``)."""
    patch = await read_patch_payload(request)
    channel = "web" if edit_session_id else "api"
    v = await svc.apply_patch_to_description(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=expected_version,
        patch=patch,
        base_sha256=base_sha256,
        channel=channel,
        edit_session_id=edit_session_id,
    )
    if ctx.capability_token_id is not None:
        await capability_tokens_svc.consume(ctx.session, token_id=ctx.capability_token_id)
    return VersionOut(id=task_id, version=v)


@router.post("/{task_id}/state", response_model=VersionOut)
async def set_state(
    task_id: uuid.UUID,
    body: TaskStateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    version = await svc.set_state(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
        state_id=body.state_id,
    )
    return VersionOut(id=task_id, version=version)


@router.post("/{task_id}/delete", response_model=VersionOut)
async def soft_delete(
    task_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    version = await svc.soft_delete_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=task_id, version=version)


@router.post("/{task_id}/restore", response_model=VersionOut)
async def restore(
    task_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    version = await svc.restore_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
    )
    return VersionOut(id=task_id, version=version)


@router.post("/{task_id}/archive", response_model=VersionOut)
async def archive(
    task_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    version = await svc.archive_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
        archived=True,
    )
    return VersionOut(id=task_id, version=version)


@router.post("/{task_id}/unarchive", response_model=VersionOut)
async def unarchive(
    task_id: uuid.UUID,
    body: ExpectedVersionIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    version = await svc.archive_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        expected_version=body.expected_version,
        archived=False,
    )
    return VersionOut(id=task_id, version=version)


@router.get("/{task_id}/reminders", response_model=list[ReminderOut])
async def list_reminders(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[ReminderOut]:
    rows = await notif_svc.list_reminders(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return [
        ReminderOut(
            id=r.id, task_id=r.task_id, offset_minutes=r.offset_minutes, channels=r.channels
        )
        for r in rows
    ]


@router.post("/{task_id}/reminders", response_model=ReminderOut)
async def add_reminder(
    task_id: uuid.UUID,
    body: ReminderIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ReminderOut:
    r = await notif_svc.add_reminder(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        offset_minutes=body.offset_minutes,
        channels=[c.value for c in body.channels] if body.channels else None,
    )
    return ReminderOut(
        id=r.id, task_id=r.task_id, offset_minutes=r.offset_minutes, channels=r.channels
    )


@router.delete(
    "/{task_id}/reminders/{reminder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_reminder(
    task_id: uuid.UUID,
    reminder_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await notif_svc.remove_reminder(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        reminder_id=reminder_id,
    )


@router.post("/{task_id}/tags", status_code=status.HTTP_204_NO_CONTENT)
async def attach_tag(
    task_id: uuid.UUID,
    body: TagRefIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.attach_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        tag_id=body.tag_id,
    )


@router.delete("/{task_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_tag(
    task_id: uuid.UUID,
    tag_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.detach_tag(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        tag_id=tag_id,
    )


@router.post("/{task_id}/assignees", status_code=status.HTTP_204_NO_CONTENT)
async def assign(
    task_id: uuid.UUID,
    body: AssigneeIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.assign(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        user_id=body.user_id,
    )


@router.delete(
    "/{task_id}/assignees/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unassign(
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await svc.unassign(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        user_id=user_id,
    )


@router.post("/{task_id}/comments", response_model=AnnotationOut)
async def add_comment(
    task_id: uuid.UUID,
    body: CommentCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> AnnotationOut:
    c = await svc.add_comment(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        body=body.body,
    )
    return await annotation_out_one(ctx.session, ctx.org_id, c)


@router.get("/{task_id}/comments", response_model=list[AnnotationOut])
async def list_comments(
    task_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> list[AnnotationOut]:
    rows = await svc.list_comments(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return await annotations_out(ctx.session, ctx.org_id, rows)


# Participants on appointment-tasks (migration 0095/0096, ADR-0008
# addendum). Only available when the task carries start_at +
# duration_minutes; pinning to a plain task / reminder returns 422
# (DomainError -> 422). Overlap rejection surfaces as 409 with
# MessageCode.EVENT_OVERLAP, same code as the assignee-axis check.
@router.get("/{task_id}/participants", response_model=list[ParticipantOut])
async def list_participants_endpoint(
    task_id: uuid.UUID, ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")]
) -> list[ParticipantOut]:
    rows = await part_svc.list_participants(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return [
        ParticipantOut(
            identity_id=p.identity_id,
            handle=i.handle,
            kind=i.kind.value,
            start_at=p.start_at,
            duration_minutes=p.duration_minutes,
        )
        for p, i in rows
    ]


@router.post("/{task_id}/participants", response_model=ParticipantOut)
async def add_participant_endpoint(
    task_id: uuid.UUID,
    body: ParticipantIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> ParticipantOut:
    row = await part_svc.add_participant(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        identity_id=body.identity_id,
        handle=body.handle,
    )
    # Re-read the identity so the response carries the handle/kind
    # the SPA needs to render without a second round-trip.
    from mycelium_core.services import identities as identities_svc

    identity = await identities_svc.get_identity(
        ctx.session, org_id=ctx.org_id, identity_id=row.identity_id
    )
    return ParticipantOut(
        identity_id=row.identity_id,
        handle=identity.handle,
        kind=identity.kind.value,
        start_at=row.start_at,
        duration_minutes=row.duration_minutes,
    )


@router.delete(
    "/{task_id}/participants/{identity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_participant_endpoint(
    task_id: uuid.UUID,
    identity_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await part_svc.remove_participant(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        identity_id=identity_id,
    )


@router.post("/{task_id}/attachments", response_model=AttachmentOut)
async def upload_task_attachment(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(task_attachment_write_ctx, scope="function")],
    file: upload_file_field,
) -> AttachmentOut:
    # Size enforced BEFORE storing (guarded read + service re-check),
    # against the workspace's effective cap. Member-level (notes/tasks
    # are member-level), org-scoped via RLS. Auth accepts a normal bearer
    # or a single-use ``attachment:write`` capability scoped to this task
    # (minted by the MCP ``upload_attachment_capability`` tool), consumed
    # on success. Backend-agnostic via ``add_attachment`` (default ``pg``
    # store, no S3 required).
    data = await read_capped(file, ctx)
    att = await att_svc.add_attachment(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        filename=file.filename or "file",
        mime_type=file.content_type,
        data=data,
    )
    if ctx.capability_token_id is not None:
        await capability_tokens_svc.consume(ctx.session, token_id=ctx.capability_token_id)
    return att_out(att)


@router.get("/{task_id}/attachments", response_model=list[AttachmentOut])
async def list_task_attachments(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[AttachmentOut]:
    rows = await att_svc.list_attachments(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return [att_out(r) for r in rows]


@router.post("/{task_id}/note", response_model=NoteOut)
async def get_or_create_task_note(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> NoteOut:
    # Member-level (notes/tasks are member-level): open the task's work
    # note, creating it on first call. Idempotent. Time spent there is
    # billed to the task via the task-scoped timer (no new model).
    n = await notes_svc.get_or_create_work_note(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
    )
    tagmap = await notes_svc.tags_by_note(ctx.session, note_ids=[n.id])
    pid = await note_links_svc.primary_task_id_for_note(
        ctx.session, org_id=ctx.org_id, note_id=n.id
    )
    titles = await note_links_svc.task_titles_for_ids(
        ctx.session, org_id=ctx.org_id, task_ids=[pid] if pid else []
    )
    return _note_out(
        n,
        tagmap.get(n.id, []),
        primary_task_id=pid,
        task_title=titles.get(pid) if pid else None,
        transcript=await notes_svc.get_body(ctx.session, note_id=n.id),
    )


@router.post("/{task_id}/notes", response_model=NoteOut)
async def create_task_note(
    task_id: uuid.UUID,
    body: TaskNoteCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> NoteOut:
    # TASK-side of the bidirectional Proposal A link: create a *fresh*
    # work note pre-linked to the task (NOT idempotent, unlike the
    # singleton /note above). Member-level, org-scoped via RLS. Time
    # logged in the note rolls up to the task.
    n = await notes_svc.create_note_for_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        title=body.title,
        text=body.text,
    )
    tagmap = await notes_svc.tags_by_note(ctx.session, note_ids=[n.id])
    pid = await note_links_svc.primary_task_id_for_note(
        ctx.session, org_id=ctx.org_id, note_id=n.id
    )
    titles = await note_links_svc.task_titles_for_ids(
        ctx.session, org_id=ctx.org_id, task_ids=[pid] if pid else []
    )
    return _note_out(
        n,
        tagmap.get(n.id, []),
        primary_task_id=pid,
        task_title=titles.get(pid) if pid else None,
        transcript=await notes_svc.get_body(ctx.session, note_id=n.id),
    )


def _note_task_link_out(link: Any) -> NoteTaskLinkOut:
    return NoteTaskLinkOut(
        id=link.id,
        note_id=link.note_id,
        task_id=link.task_id,
        kind=link.kind,
        created_by=link.created_by,
        created_at=link.created_at,
    )


@router.get(
    "/{task_id}/note-links",
    response_model=TaskNoteLinksOut,
    tags=["garden"],
)
async def list_task_note_links(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TaskNoteLinksOut:
    """Symmetric to ``GET /notes/{note_id}/links`` but task-side: every
    typed note↔task link touching ``task_id`` (all four kinds). The
    drawer pairs each link with a note title fetched separately so the
    payload stays slim."""
    links = await note_links_svc.list_note_task_links(
        ctx.session, org_id=ctx.org_id, task_id=task_id
    )
    return TaskNoteLinksOut(
        task_id=task_id,
        note_links=[_note_task_link_out(li) for li in links],
    )


@router.post(
    "/{task_id}/note-links",
    response_model=NoteTaskLinkOut,
    tags=["garden"],
)
async def add_task_note_link(
    task_id: uuid.UUID,
    body: TaskNoteLinkIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    claims: Annotated[dict[str, Any], Depends(current_claims)],
) -> NoteTaskLinkOut:
    """Task-side mirror of ``POST /notes/{note_id}/task-links``. Only
    ``subject`` / ``artifact`` accepted; ``derived_from`` and
    ``promoted_from`` are emitted only by the dedicated creation
    endpoints on the note side. The exact scope is enforced per ``kind``
    (subject = notes:write, artifact = tasks:write) behind the route's any-of
    gate (task c19f2f63, review #5)."""
    if body.kind == "subject":
        require_agent_scope(claims, "notes:write")
        link = await note_links_svc.start_task_on_note(
            ctx.session,
            org_id=ctx.org_id,
            actor_id=ctx.user_id,
            task_id=task_id,
            note_id=body.note_id,
        )
    elif body.kind == "artifact":
        require_agent_scope(claims, "tasks:write")
        link = await note_links_svc.record_task_artifact(
            ctx.session,
            org_id=ctx.org_id,
            actor_id=ctx.user_id,
            task_id=task_id,
            note_id=body.note_id,
        )
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return _note_task_link_out(link)


@router.delete(
    "/{task_id}/note-links",
    status_code=204,
    tags=["garden"],
)
async def remove_task_note_link(
    task_id: uuid.UUID,
    note_id: uuid.UUID,
    kind: str,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    """Task-side delete. Same semantics as the note-side endpoint
    (``promoted_from`` refused, idempotent 404 only if nothing matched)."""
    removed = await note_links_svc.unlink_note_task(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        note_id=note_id,
        task_id=task_id,
        kind=kind,
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


# --- P4: coordination handoffs + contract-net (docs/adr/0025) ---


def _handoff_out(h: TaskHandoff) -> HandoffOut:
    return HandoffOut(
        id=h.id,
        predecessor_task_id=h.predecessor_task_id,
        successor_task_id=h.successor_task_id,
        from_executor_id=h.from_executor_id,
        to_executor_id=h.to_executor_id,
        message=h.message,
        artifact_note_id=h.artifact_note_id,
        status=h.status,
        delivered_at=h.delivered_at,
        consumed_at=h.consumed_at,
        version=h.version,
    )


async def _task_out(ctx: TenantCtx, task: Task) -> TaskOut:
    names = await _state_names(ctx, {task.state_id})
    tagmap = await svc.tags_by_task(ctx.session, task_ids=[task.id])
    idents = await _assignee_idents(ctx, {task.id})
    creators = await _creator_idents(ctx, {task.id})
    ctokens = await _creator_tokens(ctx, {task.id})
    h, k = idents.get(task.id, (None, None))
    ch, ck, cl = _resolve_creator(task, creators, ctokens)
    # Single-task endpoints embed the checklist (the SPA task view
    # consumes it inline as the second tab). The list endpoint deliberately
    # leaves ``checklist=[]`` to avoid fan-out queries on large lists.
    items_map = await checklist_svc.items_by_task(ctx.session, task_ids=[task.id])
    return _out(
        task,
        names.get(task.state_id, ""),
        tagmap.get(task.id, []),
        assignee_handle=h,
        assignee_kind=k,
        created_by_handle=ch,
        created_by_kind=ck,
        created_by_label=cl,
        checklist=items_map.get(task.id, []),
    )


@router.get("/{task_id}/handoffs", response_model=list[HandoffOut])
async def list_handoffs(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[HandoffOut]:
    """Member: incoming + outgoing coordination handoffs for the task
    (the on-completion creation is automatic -- no create endpoint).
    RLS-scoped; a foreign task simply yields none."""
    rows = await coord_svc.list_handoffs(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return [_handoff_out(h) for h in rows]


@router.post("/{task_id}/offer", response_model=TaskOut)
async def offer_task(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TaskOut:
    """Owner: announce the task to eligible members (contract-net
    call-for-proposals). Owner-gated in the service (effective-role
    sudo enforced)."""
    task = await coord_svc.offer_task(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, task_id=task_id
    )
    return await _task_out(ctx, task)


@router.post("/{task_id}/claim", response_model=TaskOut)
async def claim_task(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TaskOut:
    """Member: claim an offered task (contract-net award) -> the caller
    becomes an assignee, ``offered`` is cleared. 400 if not offered /
    already claimed."""
    task = await coord_svc.claim_task(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, task_id=task_id
    )
    return await _task_out(ctx, task)


@router.post("/{task_id}/decline", response_model=TaskOut)
async def decline_task(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TaskOut:
    """Member: decline an offered task (lightweight: notify the offerer
    + audit; no assignment). 400 if not offered."""
    task = await coord_svc.decline_task(
        ctx.session, org_id=ctx.org_id, actor_id=ctx.user_id, task_id=task_id
    )
    return await _task_out(ctx, task)


# ---------------------------------------------------------------------------
# Checklist sub-resource: the second tab next to the markdown description
# in the SPA task view. Items are lightweight (text + done + position),
# never sub-tasks. Mutations are atomic per item so voice / agent
# automations don't have to patch the description's text.
# ---------------------------------------------------------------------------


@router.get(
    "/{task_id}/checklist",
    response_model=list[TaskChecklistItemOut],
)
async def list_checklist(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[TaskChecklistItemOut]:
    rows = await checklist_svc.list_items(ctx.session, org_id=ctx.org_id, task_id=task_id)
    return [_checklist_item_out(r) for r in rows]


@router.post(
    "/{task_id}/checklist",
    response_model=TaskChecklistItemOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_checklist_item(
    task_id: uuid.UUID,
    body: TaskChecklistItemCreateIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TaskChecklistItemOut:
    item = await checklist_svc.add_item(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        text=body.text,
        body=body.body,
        position=body.position,
    )
    return _checklist_item_out(item)


@router.patch(
    "/{task_id}/checklist/{item_id}",
    response_model=TaskChecklistItemOut,
)
async def update_checklist_item(
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    body: TaskChecklistItemPatchIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TaskChecklistItemOut:
    item = await checklist_svc.update_item(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        item_id=item_id,
        expected_version=body.expected_version,
        text=body.text,
        body=body.body,
        done=body.done,
        position=body.position,
    )
    return _checklist_item_out(item)


@router.delete(
    "/{task_id}/checklist/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_checklist_item(
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> None:
    await checklist_svc.delete_item(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        item_id=item_id,
    )


@router.post(
    "/{task_id}/checklist:clear_done",
    response_model=TaskChecklistClearDoneOut,
)
async def clear_checklist_done(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> TaskChecklistClearDoneOut:
    removed = await checklist_svc.clear_done(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
    )
    return TaskChecklistClearDoneOut(removed=removed)


@router.post(
    "/{task_id}/checklist:reorder",
    response_model=list[TaskChecklistItemOut],
)
async def reorder_checklist(
    task_id: uuid.UUID,
    body: TaskChecklistReorderIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> list[TaskChecklistItemOut]:
    rows = await checklist_svc.reorder_items(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        ordered_ids=body.ids,
    )
    return [_checklist_item_out(r) for r in rows]


def _revision_out(rev: Any, seq: int | None = None) -> RevisionOut:
    """Serialize an EntityRevision ORM row. ``org_id`` is dropped: the
    revision lives in the caller's tenant already (RLS) and the SPA
    doesn't need it on every row. ``seq`` is the revision's 1-based
    chronological position (the timeline's ``v{n}``), computed by the
    list endpoint; None on the single-revision GET."""
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


@router.get("/{task_id}/revisions", response_model=list[RevisionOut])
async def list_task_revisions(
    task_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[datetime.datetime | None, Query()] = None,
) -> list[RevisionOut]:
    """Timeline of revisions, most recent first. ``before`` filters on
    ``COALESCE(sealed_at, last_edit_at)`` so the open-window revision
    keeps showing up at the head of the first page."""
    # Validate existence + tenant scope before listing (RLS would also
    # filter, but a 404 on a missing task is a friendlier surface
    # error than an empty list).
    await svc.get_task(ctx.session, org_id=ctx.org_id, task_id=task_id, include_deleted=True)
    rows = await rev_svc.list_revisions(
        ctx.session,
        entity_kind=rev_svc.ENTITY_KIND_TASK,
        entity_id=task_id,
        limit=limit,
        before=before,
    )
    seqs = await rev_svc.revision_sequence(
        ctx.session,
        entity_kind=rev_svc.ENTITY_KIND_TASK,
        entity_id=task_id,
        only_ids=[r.id for r in rows],
    )
    return [_revision_out(r, seq=seqs.get(r.id)) for r in rows]


@router.get("/{task_id}/revisions/{rev_id}", response_model=RevisionOut)
async def get_task_revision(
    task_id: uuid.UUID,
    rev_id: uuid.UUID,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> RevisionOut:
    """Single revision lookup; 404 if the id doesn't belong to this
    task (defense in depth on top of RLS)."""
    rev = await rev_svc.get_revision(
        ctx.session,
        revision_id=rev_id,
        entity_kind=rev_svc.ENTITY_KIND_TASK,
        entity_id=task_id,
    )
    return _revision_out(rev)


@router.patch("/{task_id}/revisions/{rev_id}", response_model=RevisionOut)
async def update_task_revision_summary(
    task_id: uuid.UUID,
    rev_id: uuid.UUID,
    body: RevisionSummaryIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> RevisionOut:
    """Set / clear the ``summary`` label on a revision. The summary is
    metadata decoupled from the snapshot: it can change on sealed
    rows (the immutability trigger has a column allow-list since
    migration 0010). No optimistic-lock guard: a stale write merely
    overwrites the previous label."""
    rev = await rev_svc.set_summary(
        ctx.session,
        revision_id=rev_id,
        summary=body.summary,
        entity_kind=rev_svc.ENTITY_KIND_TASK,
        entity_id=task_id,
    )
    return _revision_out(rev)


@router.post("/{task_id}/revisions/{rev_id}/restore", response_model=VersionOut)
async def restore_task_revision(
    task_id: uuid.UUID,
    rev_id: uuid.UUID,
    body: RevisionRestoreIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> VersionOut:
    """Apply the snapshot's restorable fields back to the task. The
    operation is logged as a NEW sealed revision on the ``restore``
    channel with ``restored_from = rev_id``; the source revision is
    not mutated."""
    version = await svc.restore_revision(
        ctx.session,
        org_id=ctx.org_id,
        actor_id=ctx.user_id,
        task_id=task_id,
        revision_id=rev_id,
        expected_version=body.expected_version,
        fields=body.fields,
    )
    return VersionOut(id=task_id, version=version)


@router.post("/{task_id}/edit-session/seal", response_model=EditSessionSealOut)
async def seal_task_edit_session(
    task_id: uuid.UUID,
    body: EditSessionSealIn,
    ctx: Annotated[TenantCtx, Depends(tenant_ctx, scope="function")],
) -> EditSessionSealOut:
    """Client-initiated seal of the open web revision for the given
    ``edit_session_id``. Idempotent: closing an already-sealed (or
    never-opened) session returns ``sealed = 0``."""
    count = await rev_svc.seal_open(
        ctx.session,
        entity_kind=rev_svc.ENTITY_KIND_TASK,
        entity_id=task_id,
        actor_id=ctx.user_id,
        edit_session_id=body.edit_session_id,
    )
    return EditSessionSealOut(sealed=count)
