"""The note's operations (ADR-0068), starting with how a note is read.

Reading a note existed twice: ``GET /notes/{id}`` built ``NoteOut`` and the
``get_note`` tool built its own dict, with different fields (the tool had
the timestamps and the review state, the route had the task title, the
generated-task links and the per-part collapse state) and a different shape
for an outline. One operation and one model now serve both; each surface
keeps only its default for what it does not usually want back.

The mapping from the ORM row to the model lives here too, because the
note routes that are not bound yet still build the same ``NoteOut``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from mycelium_core.models.note import Note
from mycelium_core.models.tag import Tag
from mycelium_core.operations import Kind, OpCtx, operation, paging
from mycelium_core.operations.notes_views import (
    NoteGetIn,
    NoteListOut,
    NoteOut,
    NotePartOut,
    NotePartRow,
    NotePartsIn,
    NotesListIn,
    NotesPage,
    TagBrief,
)
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services import note_parts as parts_svc
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import text_patch

NOTES = "notes"

# --------------------------------------------------------------------------
# ORM row -> model
# --------------------------------------------------------------------------


def tag_brief(tag: Tag) -> TagBrief:
    return TagBrief(id=tag.id, kind=tag.kind, name=tag.name, color=tag.color)


def part_out(p: Any, *, ui_collapsed: bool = False) -> NotePartOut:
    """A part with its body. ``ui_collapsed`` is the caller's collapse
    state for it in the web editor; no stored row means expanded."""
    body = p.body or ""
    return NotePartOut(
        id=p.id,
        note_id=p.note_id,
        ord=p.ord,
        title=getattr(p, "title", None),
        body=body,
        body_sha256=text_patch.body_sha256(body),
        lang=p.lang,
        merged_from_note_id=p.merged_from_note_id,
        version=p.version,
        ui_collapsed=ui_collapsed,
        created_by=getattr(p, "created_by", None),
        created_at=getattr(p, "created_at", None),
        updated_at=getattr(p, "updated_at", None),
    )


def part_row(p: Any, *, with_body: bool, ui_collapsed: bool = False) -> NotePartRow:
    """A part as a list row: with its body, or as an outline (first
    non-empty line, size and digest, no body)."""
    body = p.body or ""
    head = None
    if not with_body:
        head = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")[:120]
    return NotePartRow(
        id=p.id,
        note_id=p.note_id,
        ord=p.ord,
        title=p.title,
        lang=p.lang,
        body=body if with_body else None,
        head=head,
        bytes=len(body.encode("utf-8")),
        body_sha256=text_patch.body_sha256(body),
        merged_from_note_id=p.merged_from_note_id,
        version=p.version,
        ui_collapsed=ui_collapsed,
        created_by=p.created_by,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def list_out(
    n: Note,
    tags: list[Tag] | None = None,
    primary_task_id: uuid.UUID | None = None,
    task_title: str | None = None,
    derived_task_ids: list[uuid.UUID] | None = None,
    linked_task_count: int = 0,
    preview: str | None = None,
) -> NoteListOut:
    """The fields every projection of a note shares, plus the list preview.

    ``task_id`` is the primary task from the typed link table (ADR-0029);
    ``derived_task_ids`` the tasks generated from the note;
    ``linked_task_count`` every typed link into it. ``project_id`` is the
    project-kind tag, the junction being the source of truth."""
    project_id = next(
        (t.id for t in (tags or []) if getattr(t.kind, "value", t.kind) == "project"),
        None,
    )
    return NoteListOut(
        id=n.id,
        project_id=project_id,
        task_id=primary_task_id,
        task_title=task_title,
        kind=n.kind,
        status=n.status,
        title=n.title,
        summary=n.summary,
        audio_ref=n.audio_ref,
        audio_seconds=n.audio_seconds,
        is_archived=n.is_archived,
        protected=n.protected,
        deleted_at=n.deleted_at,
        tags=[tag_brief(t) for t in (tags or [])],
        version=n.version,
        maturity=n.maturity,
        index_scope=n.index_scope,
        promoted_at=n.promoted_at,
        humus_kind=n.humus_kind,
        derived_task_ids=list(derived_task_ids or []),
        linked_task_count=linked_task_count,
        created_at=n.created_at,
        updated_at=n.updated_at,
        review_state=n.review_state,
        preview=preview,
    )


def note_out(
    n: Note,
    tags: list[Tag] | None = None,
    primary_task_id: uuid.UUID | None = None,
    task_title: str | None = None,
    derived_task_ids: list[uuid.UUID] | None = None,
    linked_task_count: int = 0,
    parts: list[NotePartOut] | None = None,
    transcript: str | None = None,
    include_transcript: bool = True,
    outline: list[NotePartRow] | None = None,
) -> NoteOut:
    """The single-note projection: the shared fields plus the body.

    ``transcript`` is the join of the part bodies when the parts are at
    hand, else what the caller passes; a response carrying ``parts`` then
    carries the note twice, which is why a reader may decline it."""
    base = list_out(n, tags, primary_task_id, task_title, derived_task_ids, linked_task_count)
    joined = "\n\n".join(p.body or "" for p in parts) if parts else transcript
    return NoteOut(
        **base.model_dump(exclude={"preview", "transcript"}),
        transcript=joined if include_transcript else None,
        parts=list(parts or []),
        outline=outline,
    )


# --------------------------------------------------------------------------
# Reading a note
# --------------------------------------------------------------------------


@operation(
    "notes.get",
    domain=NOTES,
    kind=Kind.READ,
    access="notes:read",
    params=NoteGetIn,
    result=NoteOut,
)
async def get(ctx: OpCtx, p: NoteGetIn) -> NoteOut:
    """Read one note with its ordered ``parts`` (markdown blocks). Each part
    carries its ``version`` and ``body_sha256``, the two values a guarded
    edit echoes, and who wrote it when.

    For a long note pass ``include_part_bodies=false``: ``outline`` then
    lists each part's id, title, size, first line and digest, with no
    bodies, and you fetch only the parts you need. ``transcript`` is the
    join of the part bodies: a reader of ``parts`` does not need it."""
    n = await notes_svc.get_note(ctx.session, org_id=ctx.org_id, note_id=p.note_id)
    tagmap = await notes_svc.tags_by_note(ctx.session, note_ids=[n.id])
    pid = await note_links_svc.primary_task_id_for_note(
        ctx.session, org_id=ctx.org_id, note_id=n.id
    )
    derived = await note_links_svc.derived_task_ids_for_notes(
        ctx.session, org_id=ctx.org_id, note_ids=[n.id]
    )
    counts = await note_links_svc.linked_task_counts_for_notes(
        ctx.session, org_id=ctx.org_id, note_ids=[n.id]
    )
    titles = await note_links_svc.task_titles_for_ids(
        ctx.session, org_id=ctx.org_id, task_ids=[pid] if pid else []
    )
    rows = await parts_svc.list_parts(ctx.session, org_id=ctx.org_id, note_id=n.id)
    ui = await parts_svc.get_ui_states_for_user(ctx.session, user_id=ctx.actor_id, note_id=n.id)
    parts: list[NotePartOut] | None = None
    outline: list[NotePartRow] | None = None
    if p.include_part_bodies:
        parts = [part_out(r, ui_collapsed=ui.get(r.id, False)) for r in rows]
    else:
        outline = [part_row(r, with_body=False, ui_collapsed=ui.get(r.id, False)) for r in rows]
    return note_out(
        n,
        tagmap.get(n.id, []),
        primary_task_id=pid,
        task_title=titles.get(pid) if pid else None,
        derived_task_ids=derived.get(n.id, []),
        linked_task_count=counts.get(n.id, 0),
        parts=parts,
        include_transcript=p.include_transcript and p.include_part_bodies,
        outline=outline,
    )


@operation(
    "notes.parts.list",
    domain=NOTES,
    kind=Kind.READ,
    access="notes:read",
    params=NotePartsIn,
    result=list[NotePartRow],
)
async def parts_list(ctx: OpCtx, p: NotePartsIn) -> list[NotePartRow]:
    """A note's parts in order. With ``include_body=false`` each part is its
    outline (title, size, first line, digest, no body): the table of
    contents of a long note, to pick the part to read or edit."""
    rows = await parts_svc.list_parts(ctx.session, org_id=ctx.org_id, note_id=p.note_id)
    ui = await parts_svc.get_ui_states_for_user(
        ctx.session, user_id=ctx.actor_id, note_id=p.note_id
    )
    return [part_row(r, with_body=p.include_body, ui_collapsed=ui.get(r.id, False)) for r in rows]


# --------------------------------------------------------------------------
# Listing notes
# --------------------------------------------------------------------------


@operation(
    "notes.list",
    domain=NOTES,
    kind=Kind.READ,
    access="notes:read",
    params=NotesListIn,
    result=NotesPage,
)
async def list_(ctx: OpCtx, p: NotesListIn) -> NotesPage:
    """List notes, newest first, one page at a time: pass ``next_cursor``
    back as ``cursor`` for the next disjoint page. ``q`` filters the whole
    corpus server-side before paging, so it finds a note however old; for
    semantic or cross-kind retrieval use ``search``. ``ids`` reads given
    notes; ``task_id`` the notes linked to a task. ``only`` is the bin or
    the archive. Each row has a one-line ``preview``, not the body. Date
    filters take an ISO instant or a bare date, read as a day in your
    timezone; ``order_by`` other than the default returns one page."""
    after: tuple[dt.datetime, uuid.UUID] | None = None
    if p.cursor:
        try:
            created, row_id = paging.decode_cursor(p.cursor)
            after = (dt.datetime.fromisoformat(created), uuid.UUID(row_id))
        except (ValueError, TypeError) as exc:
            raise paging.invalid("notes.list", "cursor: not one this list issued") from exc
    order_by = None if after is not None else p.order_by
    note_ids: list[uuid.UUID] | None = list(p.ids) if p.ids is not None else None
    if p.task_id is not None:
        linked = await note_links_svc.notes_for_task(
            ctx.session,
            org_id=ctx.org_id,
            task_id=p.task_id,
            kinds=tuple(p.link_kinds) if p.link_kinds else None,
            include_deleted=p.include_deleted or p.only == "deleted",
        )
        note_ids = linked if note_ids is None else [i for i in note_ids if i in set(linked)]
    if note_ids is not None and not note_ids:
        return NotesPage(items=[])
    dated = (p.created_after, p.created_before, p.updated_since)
    tz = await paging.caller_tz(ctx.session, ctx.actor_id) if any(dated) else dt.UTC
    try:
        created_from = paging.to_instant(p.created_after, tz) if p.created_after else None
        created_to = paging.to_instant(p.created_before, tz) if p.created_before else None
        updated_since = paging.to_instant(p.updated_since, tz) if p.updated_since else None
    except ValueError as exc:
        raise paging.invalid("notes.list", f"date filter: {exc}") from exc
    maturities = [m for m in ([p.maturity] if p.maturity else []) + (p.maturities or [])]
    rows = await notes_svc.list_notes(
        ctx.session,
        org_id=ctx.org_id,
        project_id=p.project_id,
        tag_id=p.tag_id,
        any_tag_ids=p.any_tag_ids,
        note_ids=note_ids,
        maturities=maturities or None,
        q=p.q,
        include_archived=p.include_archived,
        include_deleted=p.include_deleted,
        only=p.only,
        limit=p.limit + 1,
        after=after,
        created_from=created_from,
        created_to=created_to,
        updated_since=updated_since,
        order_by=order_by,
        order_desc=p.order_desc,
    )
    items, next_cursor, truncated = paging.page(rows, p.limit, key=lambda n: [n.created_at, n.id])
    if order_by is not None:
        next_cursor = None
    ids = [n.id for n in items]
    tagmap = await notes_svc.tags_by_note(ctx.session, note_ids=ids)
    pid_map = await note_links_svc.primary_task_ids_for_notes(
        ctx.session, org_id=ctx.org_id, note_ids=ids
    )
    derived_map = await note_links_svc.derived_task_ids_for_notes(
        ctx.session, org_id=ctx.org_id, note_ids=ids
    )
    count_map = await note_links_svc.linked_task_counts_for_notes(
        ctx.session, org_id=ctx.org_id, note_ids=ids
    )
    title_map = await note_links_svc.task_titles_for_ids(
        ctx.session, org_id=ctx.org_id, task_ids=list(pid_map.values())
    )
    previews = await notes_svc._previews_by_note(ctx.session, note_ids=ids)
    bodies = (
        await notes_svc._bodies_by_note(ctx.session, note_ids=ids) if p.include_transcript else {}
    )
    out: list[NoteListOut] = []
    for n in items:
        pid = pid_map.get(n.id)
        row = list_out(
            n,
            tagmap.get(n.id, []),
            primary_task_id=pid,
            task_title=title_map.get(pid) if pid else None,
            derived_task_ids=derived_map.get(n.id, []),
            linked_task_count=count_map.get(n.id, 0),
            preview=previews.get(n.id),
        )
        if p.include_transcript:
            row.transcript = bodies.get(n.id)
        out.append(row)
    return NotesPage(items=out, next_cursor=next_cursor, truncated=truncated)
