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

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.ai_providers import (
    LLMProvider,
    TranscriptionProvider,
    TtsProvider,
    get_llm,
    get_stt,
    get_tts,
)
from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.billing import CostBasis
from mycelium_core.models.classification_job import ClassificationJob
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note, NoteKind, NoteStatus, NoteTurn, TurnRole
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.project_profile import ProjectProfile
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.models.task import Task
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.services import audit, billing, lifecycle, taxonomy
from mycelium_core.services import entity_revisions as _revisions
from mycelium_core.services import memory as memory_svc
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services.rbac import require_role


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


# order_by whitelist (task 39e98a30): a strict name->ORM-column map so the
# sort key can never be raw-string-interpolated.
_NOTE_ORDER: dict[str, Any] = {
    "created_at": Note.created_at,
    "updated_at": Note.updated_at,
    "title": Note.title,
}


async def list_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    limit: int = 200,
    include_archived: bool = False,
    include_deleted: bool = False,
    project_id: uuid.UUID | None = None,
    tag_id: uuid.UUID | None = None,
    note_ids: Sequence[uuid.UUID] | None = None,
    maturities: Sequence[str] | None = None,
    q: str | None = None,
    created_from: dt.datetime | None = None,
    created_to: dt.datetime | None = None,
    updated_since: dt.datetime | None = None,
    order_by: str | None = None,
    order_desc: bool = False,
    after: tuple[dt.datetime, uuid.UUID] | None = None,
) -> list[Note]:
    """Notes in the workspace, newest first (for the @note picker and
    the notes list). RLS scopes to the org. Archived/deleted are
    excluded unless explicitly requested (trash & archive view).
    ``project_id`` / ``tag_id`` organize the list (project focus, tag
    filter). ``q`` is a free-text filter applied server-side over the
    WHOLE corpus (so it is not capped to the ``limit`` newest rows the
    way a client-side filter would be): each whitespace term must match
    the note title, any part body/title, or a tag name (terms ANDed,
    fields ORed, case-insensitive)."""
    from mycelium_core.models.note_part import NotePart

    stmt = select(Note)
    if not include_deleted:
        stmt = stmt.where(Note.deleted_at.is_(None))
    if not include_archived:
        stmt = stmt.where(Note.is_archived.is_(False))
    # ADR-0043 D2: an autonomously-generated proposal awaiting human review is
    # never listed here (only the review inbox surfaces it). NULL/'approved'
    # pass via IS DISTINCT FROM; a no-op until a proposed note exists.
    stmt = stmt.where(Note.review_state.is_distinct_from("proposed"))
    if note_ids is not None:
        # Explicit id set (e.g. the notes linked to a task): narrow to it
        # while keeping all the visibility/maturity/sort logic below.
        stmt = stmt.where(Note.id.in_(note_ids))
    if maturities:
        stmt = stmt.where(Note.maturity.in_(list(maturities)))
    if project_id is not None:
        # Project lives in the junction (migration 0016): a project
        # focus is just a tag filter against the project tag.
        stmt = stmt.where(Note.id.in_(select(NoteTag.note_id).where(NoteTag.tag_id == project_id)))
    if tag_id is not None:
        stmt = stmt.where(Note.id.in_(select(NoteTag.note_id).where(NoteTag.tag_id == tag_id)))
    if q is not None:
        for term in (w for w in q.split() if w.strip()):
            like = f"%{term}%"
            part_notes = select(NotePart.note_id).where(
                or_(NotePart.body.ilike(like), NotePart.title.ilike(like))
            )
            tag_notes = (
                select(NoteTag.note_id)
                .join(Tag, Tag.id == NoteTag.tag_id)
                .where(Tag.name.ilike(like))
            )
            stmt = stmt.where(
                or_(
                    Note.title.ilike(like),
                    Note.id.in_(part_notes),
                    Note.id.in_(tag_notes),
                )
            )
    # Date-window predicates + sort (task 39e98a30); half-open [from, to).
    if created_from is not None:
        stmt = stmt.where(Note.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(Note.created_at < created_to)
    if updated_since is not None:
        stmt = stmt.where(Note.updated_at >= updated_since)
    # Keyset cursor for the DEFAULT order (created_at desc, id asc -- both
    # NOT NULL): rows strictly after the cursor position. The MCP layer only
    # passes ``after`` for the default order, so no NULL-ordering hazard.
    if after is not None:
        ac, ai = after
        stmt = stmt.where(or_(Note.created_at < ac, and_(Note.created_at == ac, Note.id > ai)))
    order_col = _NOTE_ORDER.get(order_by) if order_by else None
    if order_col is not None:
        # id is the unique final tiebreak for a TOTAL order (stable pagination).
        stmt = stmt.order_by(
            (order_col.desc() if order_desc else order_col.asc()).nulls_last(),
            Note.created_at.desc(),
            Note.id.asc(),
        )
    else:
        stmt = stmt.order_by(Note.created_at.desc(), Note.id.asc())
    stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    include_deleted: bool = False,
    include_proposed: bool = False,
) -> Note:
    # ADR-0043 D2: a 'proposed' note (autonomously generated, pending human
    # review) is not openable through the normal read path -- it is visible
    # only via the review inbox. ``include_proposed`` is the inbox/approve
    # bypass (the review service loads via session.get and does not depend on
    # this), mirroring ``include_deleted``.
    n = (await session.execute(select(Note).where(Note.id == note_id))).scalar_one_or_none()
    if (
        n is None
        or (n.deleted_at is not None and not include_deleted)
        or (n.review_state == "proposed" and not include_proposed)
    ):
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return n


async def project_tag_for_note(session: AsyncSession, *, note_id: uuid.UUID) -> uuid.UUID | None:
    """The note's project tag id (or ``None`` if unset). Mirrors how
    tasks find their project from ``task_tags``: the project is just
    the project-kind tag in the junction. Migration 0016 dropped the
    legacy ``notes.project_id`` column."""
    return (
        await session.execute(
            select(Tag.id)
            .join(NoteTag, NoteTag.tag_id == Tag.id)
            .where(NoteTag.note_id == note_id, Tag.kind == TagKind.project)
            .limit(1)
        )
    ).scalar_one_or_none()


async def project_tag_ids_for_notes(
    session: AsyncSession, *, note_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Batched note_id -> project_tag_id; the list endpoint pays one
    query for the project chip instead of an N+1 per row."""
    out: dict[uuid.UUID, uuid.UUID] = {}
    if not note_ids:
        return out
    rows = await session.execute(
        select(NoteTag.note_id, Tag.id)
        .join(Tag, Tag.id == NoteTag.tag_id)
        .where(NoteTag.note_id.in_(note_ids), Tag.kind == TagKind.project)
    )
    for nid, tid in rows.all():
        # First project tag wins (contract: a note has at most one).
        out.setdefault(nid, tid)
    return out


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


async def get_body(session: AsyncSession, *, note_id: uuid.UUID) -> str:
    """Return the canonical text body of a note as a single string
    (task 1cd8bc0a Phase 6 final). The body is the concatenation of
    every ``note_part`` row ordered by ``ord``, joined by a blank
    line. Returns an empty string when the note has no parts.

    Replaces the legacy ``note.transcript`` column reads after the
    DROP. Callers that want the structured shape (with lang per part,
    individual ords, etc.) should query ``note_parts.list_parts``
    directly; this helper exists for the "I just want the flat body"
    sites (search snippets, audit log, LLM summaries)."""
    from mycelium_core.models.note_part import NotePart

    rows = (
        (
            await session.execute(
                select(NotePart.body)
                .where(NotePart.note_id == note_id)
                .order_by(NotePart.ord, NotePart.id)
            )
        )
        .scalars()
        .all()
    )
    return "\n\n".join((b or "") for b in rows)


async def _bodies_by_note(
    session: AsyncSession, *, note_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Batched ``{note_id: canonical body}`` for list endpoints.
    Single SELECT, ``\\n\\n``-joined in Python. Notes with no parts
    appear with ``""``."""
    from mycelium_core.models.note_part import NotePart

    if not note_ids:
        return {}
    rows = (
        await session.execute(
            select(NotePart.note_id, NotePart.body, NotePart.ord)
            .where(NotePart.note_id.in_(list(note_ids)))
            .order_by(NotePart.note_id, NotePart.ord, NotePart.id)
        )
    ).all()
    out: dict[uuid.UUID, list[str]] = {}
    for nid, body, _ in rows:
        out.setdefault(nid, []).append(body or "")
    return {nid: "\n\n".join(parts) for nid, parts in out.items()}


async def _upsert_part_zero(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    body: str,
) -> None:
    """Upsert the canonical part(ord=0) of a note with the given body.
    Used by ``create_note``, ``update_note`` and ``transcribe`` after
    Phase 6 drops the ``notes.transcript`` column."""
    from mycelium_core.models.note_part import NotePart

    existing = (
        await session.execute(
            select(NotePart).where(NotePart.note_id == note_id, NotePart.ord == 0)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(NotePart(org_id=org_id, note_id=note_id, ord=0, body=body))
    else:
        existing.body = body
    await session.flush()


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
    # Phase 6 final: ``transcript`` left the Note row; route a
    # restored transcript into note_part(ord=0). Other restorable
    # fields (``title``) flow through the row as before.
    transcript_target: str | None = None
    row_values: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "transcript":
            transcript_target = value or ""
        else:
            row_values[key] = value
    if not row_values and transcript_target is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if not row_values:
        # We still need to bump version + write a revision row, but
        # we have no row columns to set. Touch ``title`` to itself
        # via _note_set so the lifecycle audit logs the restore.
        current = await get_note(session, org_id=org_id, note_id=note_id)
        row_values = {"title": current.title}
    new_version = await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values=row_values,
        action="restore_revision",
        channel="restore",
        edit_session_id=None,
        restored_from=revision_id,
    )
    if transcript_target is not None:
        await _upsert_part_zero(session, org_id=org_id, note_id=note_id, body=transcript_target)
    return new_version


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
    # A transplanted note is read-only (docs/adr/0029 D2): reject title/body
    # edits with the same guard the part mutators use. Lazy import avoids the
    # notes <-> note_parts cycle (note_parts already imports notes lazily).
    from mycelium_core.services.note_parts import _assert_not_promoted

    await _assert_not_promoted(session, org_id=org_id, note_id=note_id)
    # Phase 6 final: ``text`` lands in note_part(ord=0), not in a
    # ``transcript`` column. The Note row's ``values`` carries only
    # the still-on-row fields (title, audio_ref, ...); the part write
    # follows the optimistic _note_set so a stale version aborts both.
    values: dict[str, Any] = {}
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
    # Phase 6 final: write the text edit to note_part(ord=0). The
    # _note_set call above no longer touches a ``transcript`` column
    # (it's gone in migration 0012); the part write completes the
    # body edit. Sequenced after _note_set so an optimistic version
    # conflict aborts both writes together.
    if text is not None:
        await _upsert_part_zero(session, org_id=org_id, note_id=note_id, body=text)
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
    # Phase 6 final: ``transcript`` is no longer a Note column; it
    # reads (and writes) through note_part(ord=0). ``summary`` still
    # lives on the Note row.
    if target == "transcript":
        current: str | None = await get_body(session, note_id=note_id)
    else:
        current = getattr(note, target)
    if dedupe_if_tail_matches and current and current.rstrip().endswith(text.rstrip()):
        return note.version, 0
    new_value = _collapsed_concat(current, separator, text)
    max_bytes = get_settings().note_body_max_bytes
    if len(new_value.encode("utf-8")) > max_bytes:
        raise DomainError(MessageCode.BODY_LIMIT_EXCEEDED, max_bytes=str(max_bytes))
    eff_version = expected_version if expected_version is not None else note.version
    values: dict[str, Any] = {}
    # Re-derive the title from the head of the body when the note has
    # no explicit one yet (same behaviour as update_note's blank-title
    # path), so the first paragraph appended to an empty body gives
    # the note a visible name.
    if target == "transcript" and not (note.title and note.title.strip()):
        derived = _derive_title(new_value)
        if derived:
            values["title"] = derived
    # ``summary`` is still a column on Note; transcript moved to part0.
    if target == "summary":
        values["summary"] = new_value
    # Phase 6 final: a transcript append still bumps the note's
    # version (callers rely on it for optimistic concurrency and the
    # revision timeline). When there's no other row-column to touch
    # we re-write ``title`` to itself: idempotent, but it routes
    # through ``_note_set`` so the version bump + audit + revision
    # land like before.
    if target == "transcript" and not values:
        values["title"] = note.title
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
    if target == "transcript":
        await _upsert_part_zero(session, org_id=org_id, note_id=note_id, body=new_value)
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
    elif kind is NoteKind.conversation:
        status = NoteStatus.ready
    else:  # voice
        status = NoteStatus.captured
    # Phase 6 final: text/voice content lives in note_part rows, not
    # on the Note row. ``title`` is still derived from the first line
    # of the text the caller supplied (when blank).
    if not (title and title.strip()):
        title = _derive_title(text)
    note = Note(
        org_id=org_id,
        kind=kind,
        status=status,
        title=title,
        audio_ref=audio_ref,
        audio_seconds=audio_seconds,
    )
    session.add(note)
    await session.flush()
    # Every note must belong to a client: the project's client when a
    # project is set, otherwise the "Personal" default. Stored as a
    # NoteTag so notes stay queryable/filterable by client. The
    # project tag itself is carried in the same junction (migration
    # 0016, ADR-0007 single-source-of-truth, mirrors task_tags).
    client_tag_id: uuid.UUID | None = None
    if project_id is not None:
        client_tag_id = (
            await session.execute(
                select(ProjectProfile.client_tag_id).where(ProjectProfile.tag_id == project_id)
            )
        ).scalar_one_or_none()
        session.add(NoteTag(org_id=org_id, note_id=note.id, tag_id=project_id))
    if client_tag_id is None:
        client_tag_id = await taxonomy.ensure_default_client(
            session, org_id=org_id, actor_id=actor_id
        )
    session.add(NoteTag(org_id=org_id, note_id=note.id, tag_id=client_tag_id))
    await session.flush()
    # Phase 6 final: the text the caller supplied lands in
    # note_part(ord=0). The Note row no longer carries a transcript
    # column; the parts table is the source of truth.
    if text:
        await _upsert_part_zero(session, org_id=org_id, note_id=note.id, body=text)
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
    # On-create auto-classify (ADR-0042 D5): enqueue in THIS transaction so a
    # rolled-back create enqueues nothing; the garden worker drains it
    # (classify + cache the suggestions). Read-only proposals, gated off by
    # default — this does NOT gate the note, which is live now as always.
    if get_settings().garden_autoclassify_on_creation_enabled:
        session.add(ClassificationJob(org_id=org_id, node_kind="note", node_id=note.id))
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
    already present (the project tag and the client tag are
    auto-attached by ``create_note`` into the junction; migration
    0016 dropped the legacy ``notes.project_id`` column). Audit logs
    one ``attach_tag`` per newly-added row."""
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
    hierarchical memory with note provenance (ADR-0016/0020).

    ``note.audio_ref`` carries an opaque pointer of the form
    ``attachment:<uuid>``. We resolve it to the raw bytes here (rather
    than inside each STT provider) so providers stay storage-agnostic;
    fakes ignore ``audio_bytes``, the real local-whisper backend needs
    them to feed faster-whisper.
    """
    await require_role(session, org_id, actor_id, Role.member)
    note = await get_note(session, org_id=org_id, note_id=note_id)
    if note.audio_ref is None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    note.status = NoteStatus.transcribing
    await session.flush()
    provider = stt or get_stt()
    seconds = note.audio_seconds or 0
    audio_bytes: bytes | None = None
    mime_type: str | None = None
    if note.audio_ref.startswith("attachment:"):
        try:
            att_id = uuid.UUID(note.audio_ref.split(":", 1)[1])
            from mycelium_core.services import attachments as att_svc

            att = await att_svc.get_attachment(session, org_id=org_id, attachment_id=att_id)
            audio_bytes = await att_svc.read_attachment_bytes(att)
            mime_type = att.mime_type
        except Exception:
            # Resolution failure leaves audio_bytes None; providers
            # that don't need raw bytes (e.g. cloud STT keyed on a
            # public URL) keep working.
            audio_bytes = None
    res = await provider.transcribe(
        audio_ref=note.audio_ref,
        audio_seconds=seconds,
        audio_bytes=audio_bytes,
        mime_type=mime_type,
    )
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
    # Phase 6 final: the STT body lands in note_part(ord=0), not a
    # transcript column. Status flips to ``ready`` so the rest of the
    # flow (memory write, audit) sees a finalised note.
    if res.text:
        await _upsert_part_zero(session, org_id=org_id, note_id=note.id, body=res.text)
    note.status = NoteStatus.ready
    await session.flush()
    # Indexing convergence (task 9fc94327): the transcript now lives in
    # note_part(ord=0), and the per-part search index
    # (services.note_search) embeds every part at commit on the seeded
    # "note" channel. So there is no separate note-level memory write here
    # -- one blob per part, no double index for voice notes. ``embed`` is
    # retained for call-site compatibility; indexing is now unconditional
    # (decision 2026-06-09: every note is a searchable node).
    _ = embed
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
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    limit: int | None = None,
    after: tuple[int, uuid.UUID] | None = None,
) -> list[NoteTurn]:
    """Conversation turns in ``ord`` order (ord asc, id asc -- a total order).
    ``limit`` + the ``after`` keyset cursor page a long transcript."""
    stmt = select(NoteTurn).where(NoteTurn.note_id == note_id)
    if after is not None:
        ao, ai = after
        stmt = stmt.where(or_(NoteTurn.ord > ao, and_(NoteTurn.ord == ao, NoteTurn.id > ai)))
    stmt = stmt.order_by(NoteTurn.ord.asc(), NoteTurn.id.asc())
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


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
    S3 audio_ref is returned for the caller/worker to delete.

    Two provenance shapes are erased: the legacy note-level blob
    (``source_kind='note'``, written by older transcribes) and the
    per-part search blobs (``source_kind='note_part'``) that the note
    search index (services.note_search) now writes for every part. Both
    must go so an erase leaves no embedding behind."""
    from mycelium_core.models.note_part import NotePart

    await require_role(session, org_id, actor_id, Role.member)
    note = await get_note(session, org_id=org_id, note_id=note_id)
    audio_ref = note.audio_ref
    part_ids = (
        (await session.execute(select(NotePart.id).where(NotePart.note_id == note_id)))
        .scalars()
        .all()
    )
    blobs_deleted = await memory_svc.gdpr_erase(
        session,
        org_id=org_id,
        actor_id=actor_id,
        source_kind="note",
        source_id=str(note_id),
    )
    for pid in part_ids:
        blobs_deleted += await memory_svc.gdpr_erase(
            session,
            org_id=org_id,
            actor_id=actor_id,
            source_kind="note_part",
            source_id=str(pid),
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
