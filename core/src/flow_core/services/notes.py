"""Notes / conversational capture + canonical intent (docs/adr/0020,
0021, FR-16).

Capture is unmetered and offline-friendly; STT/LLM/TTS processing is
metered (ADR-0019) and idempotent by operation_id. The canonical
command grammar is deterministic, offline and unmetered; project slots
are resolved by name and ambiguity is surfaced, never mis-scoped
(ADR-0021/0007). Transcripts feed hierarchical memory with note
provenance; erasure cascades there.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.ai_providers import (
    LLMProvider,
    TranscriptionProvider,
    TtsProvider,
    get_llm,
    get_stt,
    get_tts,
)
from flow_core.config import get_settings
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.billing import CostBasis
from flow_core.models.membership import Role
from flow_core.models.note import Note, NoteKind, NoteStatus, NoteTurn, TurnRole
from flow_core.models.note_tag import NoteTag
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import Task
from flow_core.models.task_tag import TaskTag
from flow_core.services import audit, billing, lifecycle, taxonomy
from flow_core.services import entity_revisions as _revisions
from flow_core.services import memory as memory_svc
from flow_core.services import note_links as note_links_svc
from flow_core.services.rbac import require_role


class _Unset:
    """Sentinel: ``task_id`` not supplied to ``update_note`` (preserve)
    vs an explicit ``None`` (clear the note<->task link)."""


_UNSET = _Unset()


@dataclass(frozen=True)
class ParsedCommand:
    action: str  # create_note | start_conversation
    project_name: str | None
    raw: str


@dataclass(frozen=True)
class NoteErasure:
    audio_ref: str | None
    memory_blobs_deleted: int


_CREATE_NOTE = re.compile(r"\b(create|crea|new|nuova|nuovo)\b.*\b(note|nota)\b", re.I)
_START_CONV = re.compile(r"\b(conversation|conversazione|session|sessione)\b", re.I)
_PROJECT = re.compile(
    r"\b(?:in project|nel progetto|in progetto|project|progetto)\s+([\w .\-]+)$",
    re.I,
)


def parse_command(text: str) -> ParsedCommand:
    """Deterministic canonical grammar (no LLM, offline, unmetered).
    LLM free-form fallback is a later, metered refinement (ADR-0021)."""
    raw = text.strip()
    pm = _PROJECT.search(raw)
    project_name = pm.group(1).strip() if pm else None
    head = raw[: pm.start()] if pm else raw
    if _CREATE_NOTE.search(head):
        action = "create_note"
    elif _START_CONV.search(head):
        action = "start_conversation"
    else:
        raise DomainError(MessageCode.INTENT_UNRECOGNIZED, raw=raw)
    return ParsedCommand(action=action, project_name=project_name, raw=raw)


async def resolve_project(
    session: AsyncSession, *, org_id: uuid.UUID, name: str | None
) -> uuid.UUID | None:
    """Exact, then unambiguous fuzzy; ambiguous/unknown -> clarify.
    Never silently defaults to another project (ADR-0007/0021)."""
    if name is None:
        return None  # explicit "no project" = personal inbox scope
    exact = (
        await session.execute(
            select(Tag.id).where(
                Tag.kind == TagKind.project,
                func.lower(Tag.name) == name.lower(),
            )
        )
    ).scalar_one_or_none()
    if exact is not None:
        return exact
    fuzzy = (
        (
            await session.execute(
                select(Tag.id).where(
                    Tag.kind == TagKind.project,
                    Tag.name.ilike(f"%{name}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(fuzzy) == 1:
        return fuzzy[0]
    if not fuzzy:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    raise DomainError(MessageCode.TAG_AMBIGUOUS, name=name)


def _derive_title(text: str | None) -> str | None:
    """Apple Notes style: when no title is given, the first non-empty
    line of the body becomes the title (trimmed, capped at 120)."""
    if not text:
        return None
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:120]
    return None


async def list_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    limit: int = 200,
    include_archived: bool = False,
    include_deleted: bool = False,
    project_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
) -> list[Note]:
    """Notes in the workspace, newest first (for the @note picker and
    the notes list). RLS scopes to the org. Archived/deleted are
    excluded unless explicitly requested (trash & archive view).
    ``project_id`` / ``tag_id`` organize the list (project focus, tag
    filter)."""
    stmt = select(Note)
    if not include_deleted:
        stmt = stmt.where(Note.deleted_at.is_(None))
    if not include_archived:
        stmt = stmt.where(Note.is_archived.is_(False))
    if project_id is not None:
        stmt = stmt.where(Note.project_id == project_id)
    if tag_id is not None:
        stmt = stmt.where(Note.id.in_(select(NoteTag.note_id).where(NoteTag.tag_id == tag_id)))
    stmt = stmt.order_by(Note.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    include_deleted: bool = False,
) -> Note:
    n = (await session.execute(select(Note).where(Note.id == note_id))).scalar_one_or_none()
    if n is None or (n.deleted_at is not None and not include_deleted):
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return n


async def tags_by_note(
    session: AsyncSession, *, note_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[Tag]]:
    """Batched note -> tags (chips in the notes list without an N+1)."""
    out: dict[uuid.UUID, list[Tag]] = {}
    if not note_ids:
        return out
    rows = await session.execute(
        select(NoteTag.note_id, Tag)
        .join(Tag, Tag.id == NoteTag.tag_id)
        .where(NoteTag.note_id.in_(note_ids))
    )
    for nid, tag in rows.all():
        out.setdefault(nid, []).append(tag)
    return out


async def attach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await get_note(session, org_id=org_id, note_id=note_id)
    tag = (await session.execute(select(Tag.id).where(Tag.id == tag_id))).scalar_one_or_none()
    if tag is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    try:
        async with session.begin_nested():
            session.add(NoteTag(org_id=org_id, note_id=note_id, tag_id=tag_id))
            await session.flush()
    except IntegrityError:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="attach_tag",
    )


async def detach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(NoteTag).where(NoteTag.note_id == note_id, NoteTag.tag_id == tag_id)
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="detach_tag",
    )


async def _log_note_revision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    version_from: int,
    version_to: int,
    changed_fields: list[str],
    channel: str,
    edit_session_id: str | None,
    restored_from: uuid.UUID | None = None,
) -> None:
    """Recovery-history entry for a note mutation. Reads the note back
    so the snapshot reflects the post-update state (the Core UPDATE
    inside ``optimistic_update`` bypasses the ORM mapper)."""
    fresh = await get_note(session, org_id=org_id, note_id=note_id, include_deleted=True)
    snapshot = await _revisions.snapshot_note(session, fresh)
    await _revisions.append(
        session,
        org_id=org_id,
        entity_kind=_revisions.ENTITY_KIND_NOTE,
        entity_id=note_id,
        actor_id=actor_id,
        snapshot=snapshot,
        changed_fields=changed_fields,
        channel=channel,
        version_from=version_from,
        version_to=version_to,
        edit_session_id=edit_session_id,
        restored_from=restored_from,
    )


async def _note_set(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
    action: str,
    channel: str = "system",
    edit_session_id: str | None = None,
    restored_from: uuid.UUID | None = None,
) -> int:
    # Validate existence (include deleted: restore needs to see the
    # soft-deleted row). Flag flip + audit shared with tasks via
    # lifecycle.transition.
    await get_note(session, org_id=org_id, note_id=note_id, include_deleted=True)
    new_version = await lifecycle.transition(
        session,
        model_cls=Note,
        org_id=org_id,
        actor_id=actor_id,
        entity_id=note_id,
        expected_version=expected_version,
        values=values,
        audit_entity="note",
        audit_action=action,
    )
    # ``_note_set`` is the shared entry point for both content edits
    # (update_note) and lifecycle transitions (archive/delete/restore).
    # The caller picks the right ``changed_fields`` tag through
    # ``action``; content edits add the actual column names so the
    # timeline still shows what was touched.
    if action == "update":
        fields = list(values.keys())
    else:
        fields = [f"_{action}", *values.keys()]
    await _log_note_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        version_from=expected_version,
        version_to=new_version,
        changed_fields=fields,
        channel=channel,
        edit_session_id=edit_session_id,
        restored_from=restored_from,
    )
    return new_version


async def restore_revision(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    revision_id: uuid.UUID,
    expected_version: int,
    fields: Sequence[str] | None = None,
) -> int:
    """Revert a note's restorable fields (``title`` / ``transcript``)
    to the snapshot stored in ``revision_id``. Produces a NEW sealed
    revision on the ``restore`` channel with ``restored_from``
    pointing at the source revision."""
    revision = await _revisions.get_revision(
        session,
        revision_id=revision_id,
        entity_kind=_revisions.ENTITY_KIND_NOTE,
        entity_id=note_id,
    )
    payload = _revisions.restorable_payload(revision, fields=fields)
    if not payload:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    # The snapshot stores the column name ``transcript``; that matches
    # the DB column so it lands straight in ``values``. No type
    # coercion needed: both restorable fields are plain strings.
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values=dict(payload),
        action="restore_revision",
        channel="restore",
        edit_session_id=None,
        restored_from=revision_id,
    )


async def update_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
    title: str | None = None,
    text: str | None = None,
    task_id: uuid.UUID | None | _Unset = _UNSET,
    audio_ref: str | None | _Unset = _UNSET,
    # Recovery history. ``channel='web'`` plus an ``edit_session_id``
    # coalesces consecutive PATCHes from the same SPA session into a
    # single open revision (autosave-friendly). Other channels write
    # sealed-on-arrival rows.
    channel: str = "api",
    edit_session_id: str | None = None,
) -> int:
    """Edit title/body. When the title is blank it is re-derived from
    the first line of the body (Apple Notes style).

    Bidirectional Proposal A link: ``task_id`` (when supplied) sets OR
    clears ``notes.task_id`` (an explicit ``None`` unlinks). A target
    task is validated in-org (RLS scopes the lookup); TASK_NOT_FOUND if
    absent. Omitting the argument leaves the existing link untouched."""
    values: dict[str, Any] = {}
    if text is not None:
        values["transcript"] = text
    eff_title = title if (title and title.strip()) else _derive_title(text)
    values["title"] = eff_title
    # docs/adr/0029 P3: ``note.task_id`` is gone. The same setter API
    # now writes (or clears) the ``artifact`` typed link.
    pending_task_link: tuple[bool, uuid.UUID | None] = (False, None)
    if not isinstance(task_id, _Unset):
        if task_id is not None:
            exists = (
                await session.execute(
                    select(Task.id).where(Task.id == task_id, Task.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if exists is None:
                raise NotFoundError(MessageCode.TASK_NOT_FOUND)
        pending_task_link = (True, task_id)
    if not isinstance(audio_ref, _Unset):
        # Voice-note capture (Telegram bot / PWA recorder) sets
        # ``audio_ref`` after the row exists -- the attachment id is
        # known only after the upload completes. Same sentinel semantic
        # as task_id: omit = no change, ``None`` = clear.
        values["audio_ref"] = audio_ref
    new_version = await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values=values,
        action="update",
        channel=channel,
        edit_session_id=edit_session_id,
    )
    # Phase 6 prep (task 1cd8bc0a): mirror the transcript edit into
    # part(ord=0). If a part(ord=0) already exists, update its body;
    # otherwise create one. We deliberately keep this transactional
    # with the optimistic _note_set above so a failed UPDATE on the
    # note doesn't leave a stale part write behind. Skipped when
    # ``text`` isn't part of this patch (the user only touched title
    # or task_id).
    if text is not None:
        from flow_core.models.note_part import NotePart as _NotePart

        existing = (
            await session.execute(
                select(_NotePart).where(
                    _NotePart.note_id == note_id, _NotePart.ord == 0
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                _NotePart(org_id=org_id, note_id=note_id, ord=0, body=text)
            )
        else:
            # ORM-style update so the identity map stays consistent
            # (a Core UPDATE bypassed the mapper and a subsequent read
            # could return the stale body cached on the loaded row).
            existing.body = text
        await session.flush()
    if pending_task_link[0]:
        new_task_id = pending_task_link[1]
        if new_task_id is None:
            await note_links_svc.clear_artifact_links(
                session, org_id=org_id, actor_id=actor_id, note_id=note_id
            )
        else:
            await note_links_svc.ensure_artifact_link(
                session,
                org_id=org_id,
                actor_id=actor_id,
                note_id=note_id,
                task_id=new_task_id,
            )
    return new_version


# --- Context-blind append (task 4ac39ecf) -----------------------------------
# Domain-semantic edit primitive for MCP / LLM callers that want to add a
# paragraph without first reading the whole body. Distinct from
# ``update_note`` (full-field replace, used by the Tiptap autosave).


_APPEND_TARGETS_NOTE: frozenset[str] = frozenset({"summary", "transcript"})


def _collapsed_concat(current: str | None, separator: str, text: str) -> str:
    """Join ``current + separator + text`` with two collapses: an empty
    current swallows the separator; a current that already ends with the
    separator (modulo trailing whitespace) doesn't get a second copy.
    This keeps appended paragraphs aligned with the existing block
    structure without introducing accidental gaps."""
    base = current or ""
    if not base:
        return text
    if base.endswith(separator):
        return base + text
    if separator and base.rstrip(" \t\n\r").endswith(separator.rstrip(" \t\n\r")):
        # The current body ends with the structural part of the separator
        # but has trailing whitespace; fall through to a single newline
        # join so we don't double-up the blank line.
        return base.rstrip() + "\n\n" + text if separator == "\n\n" else base + text
    return base + separator + text


async def append_to_note_field(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    target: str,
    text: str,
    separator: str = "\n\n",
    expected_version: int | None = None,
    dedupe_if_tail_matches: bool = False,
    channel: str = "api",
) -> tuple[int, int]:
    """Append ``text`` to ``note.transcript`` or ``note.summary`` without
    reading the body first.

    Returns ``(new_version, appended_chars)``. Idempotency contract:
    when ``dedupe_if_tail_matches=True`` and the body already ends with
    ``text`` (ignoring trailing whitespace), the write is skipped and
    the function returns ``(current_version, 0)`` -- safe for MCP
    retries.

    Concurrency: ``expected_version=None`` means "append onto whatever
    state the row currently has"; the helper loads the version and
    submits the optimistic update with it (a concurrent writer wins by
    bumping version first, here we surface ``stale_version`` rather
    than silently overwriting -- caller can retry on the new state).
    Pass an explicit ``expected_version`` to assert the caller has a
    coherent view of the row.
    """
    if target not in _APPEND_TARGETS_NOTE:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    note = await get_note(session, org_id=org_id, note_id=note_id)
    current: str | None = getattr(note, target)
    if dedupe_if_tail_matches and current and current.rstrip().endswith(text.rstrip()):
        return note.version, 0
    new_value = _collapsed_concat(current, separator, text)
    max_bytes = get_settings().note_body_max_bytes
    if len(new_value.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    eff_version = expected_version if expected_version is not None else note.version
    values: dict[str, Any] = {target: new_value}
    # Re-derive the title from the head of the body when the note has
    # no explicit one yet (same behaviour as update_note's blank-title
    # path), so the first paragraph appended to an empty transcript
    # gives the note a visible name.
    if target == "transcript" and not (note.title and note.title.strip()):
        derived = _derive_title(new_value)
        if derived:
            values["title"] = derived
    new_version = await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=eff_version,
        values=values,
        action="append",
        channel=channel,
    )
    return new_version, len(text)


async def archive_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
    archived: bool = True,
) -> int:
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values={"is_archived": archived},
        action="archive",
    )


async def soft_delete_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values={"deleted_at": dt.datetime.now(tz=dt.UTC)},
        action="delete",
    )


async def restore_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
) -> int:
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values={"deleted_at": None},
        action="restore",
    )


async def create_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    kind: NoteKind,
    project_id: uuid.UUID | None = None,
    title: str | None = None,
    text: str | None = None,
    audio_ref: str | None = None,
    audio_seconds: int | None = None,
    # Recovery history: the channel this create came in through. The
    # baseline revision is written sealed so the timeline shows the
    # note's starting point.
    channel: str = "api",
    edit_session_id: str | None = None,
) -> Note:
    """Capture only. NOT metered, works at zero credits (ADR-0020:
    never lose the idea)."""
    await require_role(session, org_id, actor_id, Role.member)
    if kind is NoteKind.text:
        status = NoteStatus.ready
        transcript = text
    elif kind is NoteKind.conversation:
        status = NoteStatus.ready
        transcript = None
    else:  # voice
        status = NoteStatus.captured
        transcript = None
    if not (title and title.strip()):
        title = _derive_title(text)
    note = Note(
        org_id=org_id,
        project_id=project_id,
        kind=kind,
        status=status,
        title=title,
        transcript=transcript,
        audio_ref=audio_ref,
        audio_seconds=audio_seconds,
    )
    session.add(note)
    await session.flush()
    # Every note must belong to a client: the project's client when a
    # project is set, otherwise the "Personal" default. Stored as a
    # NoteTag so notes stay queryable/filterable by client.
    client_tag_id: uuid.UUID | None = None
    if project_id is not None:
        client_tag_id = (
            await session.execute(
                select(ProjectProfile.client_tag_id).where(ProjectProfile.tag_id == project_id)
            )
        ).scalar_one_or_none()
    if client_tag_id is None:
        client_tag_id = await taxonomy.ensure_default_client(
            session, org_id=org_id, actor_id=actor_id
        )
    session.add(NoteTag(org_id=org_id, note_id=note.id, tag_id=client_tag_id))
    await session.flush()
    # Phase 6 prep (task 1cd8bc0a): mirror the initial transcript
    # into a part(ord=0) so the new readers (SPA NotePartsEditor,
    # MCP get_note's parts[], flow-cli) see content for every note
    # created post-deploy. A future PR flips readers to parts as
    # the source of truth and drops notes.transcript; until then we
    # double-write so the two surfaces stay in sync.
    if transcript:
        from flow_core.models.note_part import NotePart as _NotePart

        session.add(_NotePart(org_id=org_id, note_id=note.id, ord=0, body=transcript))
        await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note.id,
        action="create",
    )
    await _log_note_revision(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note.id,
        version_from=note.version,
        version_to=note.version,
        changed_fields=["_create"],
        channel=channel,
        edit_session_id=edit_session_id,
    )
    return note


async def get_or_create_work_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
) -> Note:
    """The task's single "work note": open it, write in it; time spent
    there is billed to the task (time entries stay task-scoped, no new
    model). Idempotent: the second call returns the same note. The note
    is created via ``create_note`` so the client/Personal enforcement
    runs; it inherits the task's project (hence its client) when one
    can be derived from the task's tags. ALL task tags (project,
    client, generic) are then propagated onto the note so filters and
    /focus see them on the same axes."""
    task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    # docs/adr/0029 P3: search through the typed artifact link
    # instead of the legacy ``Note.task_id`` column.
    note_ids = await note_links_svc.notes_for_task(
        session, org_id=org_id, task_id=task_id, kinds=("artifact",)
    )
    if note_ids:
        existing = (
            await session.execute(
                select(Note).where(Note.id.in_(note_ids), Note.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    # Derive the task's project tag the same way create_task does (its
    # first project-kind tag), so the work note lands under the task's
    # project/client; otherwise create_note attaches the Personal client.
    project_id = (
        await session.execute(
            select(Tag.id)
            .join(TaskTag, TaskTag.tag_id == Tag.id)
            .where(TaskTag.task_id == task_id, Tag.kind == TagKind.project)
            .limit(1)
        )
    ).scalar_one_or_none()
    note = await create_note(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=NoteKind.text,
        project_id=project_id,
        title=task.title or "Work note",
        text=None,
    )
    await session.flush()
    # docs/adr/0029 P3: the Proposal A link is the typed
    # ``artifact`` row, not a column on the note.
    await note_links_svc.ensure_artifact_link(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note.id,
        task_id=task_id,
    )
    await _copy_task_tags_to_note(
        session, org_id=org_id, actor_id=actor_id, note_id=note.id, task_id=task_id
    )
    return note


async def create_note_for_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    title: str | None = None,
    text: str | None = None,
) -> Note:
    """Create a fresh work note pre-linked to ``task_id`` (the TASK-side
    of the bidirectional Proposal A link). Unlike
    ``get_or_create_work_note`` this is NOT idempotent: each call
    creates a new note. The task must exist in-org (RLS scopes the
    lookup; TASK_NOT_FOUND otherwise). The note is created via
    ``create_note`` so the client/Personal enforcement runs, inheriting
    the task's project (hence client) when one can be derived from its
    tags; title defaults to the task title. ``notes.task_id`` is set so
    time logged in the note rolls up to this task."""
    task = (
        await session.execute(select(Task).where(Task.id == task_id, Task.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    project_id = (
        await session.execute(
            select(Tag.id)
            .join(TaskTag, TaskTag.tag_id == Tag.id)
            .where(TaskTag.task_id == task_id, Tag.kind == TagKind.project)
            .limit(1)
        )
    ).scalar_one_or_none()
    note = await create_note(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=NoteKind.text,
        project_id=project_id,
        title=title if (title and title.strip()) else (task.title or "Work note"),
        text=text,
    )
    await session.flush()
    # docs/adr/0029 P3: the Proposal A link is the typed
    # ``artifact`` row, not a column on the note.
    await note_links_svc.ensure_artifact_link(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note.id,
        task_id=task_id,
    )
    await _copy_task_tags_to_note(
        session, org_id=org_id, actor_id=actor_id, note_id=note.id, task_id=task_id
    )
    return note


async def _copy_task_tags_to_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    task_id: uuid.UUID,
) -> None:
    """Propagate every tag of ``task_id`` onto ``note_id``. Skips rows
    already present (the project tag becomes ``notes.project_id`` and
    the client tag is auto-attached by ``create_note``). Audit logs one
    ``attach_tag`` per newly-added row."""
    task_tag_ids = list(
        (await session.execute(select(TaskTag.tag_id).where(TaskTag.task_id == task_id)))
        .scalars()
        .all()
    )
    if not task_tag_ids:
        return
    existing = set(
        (await session.execute(select(NoteTag.tag_id).where(NoteTag.note_id == note_id)))
        .scalars()
        .all()
    )
    for tag_id in task_tag_ids:
        if tag_id in existing:
            continue
        try:
            async with session.begin_nested():
                session.add(NoteTag(org_id=org_id, note_id=note_id, tag_id=tag_id))
                await session.flush()
        except IntegrityError:
            continue
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=note_id,
            action="attach_tag",
        )


async def run_command(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    text: str,
) -> Note:
    """Canonical NL command -> deterministic action (ADR-0021)."""
    cmd = parse_command(text)
    project_id = await resolve_project(session, org_id=org_id, name=cmd.project_name)
    kind = NoteKind.text if cmd.action == "create_note" else NoteKind.conversation
    return await create_note(
        session,
        org_id=org_id,
        actor_id=actor_id,
        kind=kind,
        project_id=project_id,
        title=cmd.raw[:300],
    )


async def transcribe(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    operation_id: str,
    embed: bool = True,
    stt: TranscriptionProvider | None = None,
) -> Note:
    """STT processing: metered per audio-minute; the transcript feeds
    hierarchical memory with note provenance (ADR-0016/0020)."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await get_note(session, org_id=org_id, note_id=note_id)
    if note.audio_ref is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    note.status = NoteStatus.transcribing
    await session.flush()
    provider = stt or get_stt()
    seconds = note.audio_seconds or 0
    res = await provider.transcribe(audio_ref=note.audio_ref, audio_seconds=seconds)
    await billing.meter(
        session,
        org_id=org_id,
        actor_id=actor_id,
        operation_id=operation_id,
        op="stt",
        model_id=res.model_id,
        units_in=Decimal(res.audio_seconds) / Decimal(60),
        basis=CostBasis.local,
    )
    note.transcript = res.text
    note.status = NoteStatus.ready
    await session.flush()
    if embed and res.text:
        await memory_svc.write_blob(
            session,
            org_id=org_id,
            actor_id=actor_id,
            project_id=note.project_id,
            text_body=res.text,
            operation_id=f"{operation_id}:mem",
            namespace="note",
            sources=[("note", str(note.id))],
            # Note-derived memory lands on the canonical "note" channel
            # deterministically (resolved via the seeded channel_key
            # path; the channel is guaranteed seeded by
            # ensure_default_memory_channels).
            channel_key="note",
        )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note.id,
        action="transcribe",
    )
    return note


async def append_message(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    content: str,
    operation_id: str,
    llm: LLMProvider | None = None,
) -> NoteTurn:
    """Conversation turn: user message + a metered LLM reply, both
    saved on the conversation note (ADR-0020)."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await get_note(session, org_id=org_id, note_id=note_id)
    if note.kind is not NoteKind.conversation:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    turns = list(
        (
            await session.execute(
                select(NoteTurn).where(NoteTurn.note_id == note_id).order_by(NoteTurn.ord)
            )
        )
        .scalars()
        .all()
    )
    next_ord = (turns[-1].ord + 1) if turns else 0
    session.add(
        NoteTurn(
            org_id=org_id,
            note_id=note_id,
            role=TurnRole.user,
            content=content,
            ord=next_ord,
        )
    )
    await session.flush()
    history: list[tuple[str, str]] = [(t.role.value, t.content) for t in turns]
    history.append(("user", content))
    provider = llm or get_llm()
    res = await provider.complete(system=None, messages=history)
    await billing.meter(
        session,
        org_id=org_id,
        actor_id=actor_id,
        operation_id=operation_id,
        op="llm",
        model_id=res.model_id,
        units_in=Decimal(res.tokens_in),
        units_out=Decimal(res.tokens_out),
        basis=CostBasis.local,
    )
    reply = NoteTurn(
        org_id=org_id,
        note_id=note_id,
        role=TurnRole.assistant,
        content=res.text,
        ord=next_ord + 1,
    )
    session.add(reply)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="append_message",
    )
    return reply


async def list_turns(
    session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID
) -> list[NoteTurn]:
    return list(
        (
            await session.execute(
                select(NoteTurn).where(NoteTurn.note_id == note_id).order_by(NoteTurn.ord)
            )
        )
        .scalars()
        .all()
    )


async def synthesize(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    text: str,
    operation_id: str,
    tts: TtsProvider | None = None,
) -> dict[str, str]:
    """TTS voice-out: metered per character (ADR-0019/0020)."""
    await require_role(session, org_id, actor_id, Role.member)
    provider = tts or get_tts()
    res = await provider.synthesize(text=text)
    await billing.meter(
        session,
        org_id=org_id,
        actor_id=actor_id,
        operation_id=operation_id,
        op="tts",
        model_id=res.model_id,
        units_in=Decimal(res.chars),
        basis=CostBasis.local,
    )
    return {"audio_ref": res.audio_ref, "model_id": res.model_id}


async def gdpr_erase_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
) -> NoteErasure:
    """Cascade: memory blobs (by note provenance) + note + turns. The
    S3 audio_ref is returned for the caller/worker to delete."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await get_note(session, org_id=org_id, note_id=note_id)
    audio_ref = note.audio_ref
    blobs_deleted = await memory_svc.gdpr_erase(
        session,
        org_id=org_id,
        actor_id=actor_id,
        source_kind="note",
        source_id=str(note_id),
    )
    await session.delete(note)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="gdpr_erase",
        diff={"memory_blobs": str(blobs_deleted)},
    )
    return NoteErasure(audio_ref=audio_ref, memory_blobs_deleted=blobs_deleted)
