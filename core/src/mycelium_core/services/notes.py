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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
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
from mycelium_core.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    UnprocessableError,
)
from mycelium_core.i18n import MessageCode
from mycelium_core.models.billing import CostBasis
from mycelium_core.models.classification_job import ClassificationJob
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note, NoteKind, NoteStatus, NoteTurn, TurnRole
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.models.task import Task
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.services import audit, billing, lifecycle, tag_assignment
from mycelium_core.services import entity_revisions as _revisions
from mycelium_core.services import memory as memory_svc
from mycelium_core.services import note_links as note_links_svc
from mycelium_core.services.note_effective import effective_note_clause, note_is_effective
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
    # The effective-note predicate (ADR-0043 D1) with its trash opt-in: an
    # autonomously-generated proposal awaiting review is never listed here
    # (only the review inbox surfaces it), and the bin needs ``include_deleted``.
    # The archive is the separate presentation axis and keeps its own opt-in.
    stmt = stmt.where(effective_note_clause(include_deleted=include_deleted))
    if not include_archived:
        stmt = stmt.where(Note.is_archived.is_(False))
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
    if n is None or not note_is_effective(
        review_state=n.review_state,
        deleted_at=n.deleted_at,
        include_deleted=include_deleted,
        include_proposed=include_proposed,
    ):
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return n


async def project_tag_for_note(session: AsyncSession, *, note_id: uuid.UUID) -> uuid.UUID | None:
    """The note's project tag id, or ``None`` for a projectless
    (personal) note. The project is just the project-kind tag in the
    junction; migration 0016 dropped the legacy ``notes.project_id``
    column.

    Since ``services.tag_assignment`` owns the structural rows, a note
    carries AT MOST one project (docs/adr/0021): this is a single-row
    read. A second row is corrupted taxonomy and is surfaced, not
    narrowed away by an unordered LIMIT 1 that would have made the
    retrieval perimeter depend on the query plan."""
    rows = (
        (
            await session.execute(
                select(Tag.id)
                .join(NoteTag, NoteTag.tag_id == Tag.id)
                .where(NoteTag.note_id == note_id, Tag.kind == TagKind.project)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > 1:
        raise DomainError(MessageCode.TAG_MULTIPLE_PROJECTS)
    return rows[0] if rows else None


async def project_tag_ids_for_notes(
    session: AsyncSession, *, note_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Batched note_id -> project_tag_id; the list endpoint pays one
    query for the project chip instead of an N+1 per row. Notes with no
    project are simply absent from the map.

    One row per note (docs/adr/0021, enforced by tag_assignment): a
    second project on the same note is reported like the single-note
    read does, instead of letting an arbitrary row win the chip."""
    out: dict[uuid.UUID, uuid.UUID] = {}
    if not note_ids:
        return out
    rows = await session.execute(
        select(NoteTag.note_id, Tag.id)
        .join(Tag, Tag.id == NoteTag.tag_id)
        .where(NoteTag.note_id.in_(note_ids), Tag.kind == TagKind.project)
    )
    for nid, tid in rows.all():
        if out.setdefault(nid, tid) != tid:
            raise DomainError(MessageCode.TAG_MULTIPLE_PROJECTS)
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
    """Attach one tag, dispatched on its kind. A free-form facet is just
    added; a PROJECT tag is a MOVE (the note follows that project's
    client, atomically); a CLIENT tag re-points the note and is refused
    when it contradicts the attached project.

    Every client/project junction write goes through tag_assignment,
    which also re-scopes the note's indexed blobs on a project change
    (the ADR-0007 retrieval perimeter, task 1d152747) and writes the
    audit row -- hence none here."""
    await require_role(session, org_id, actor_id, Role.member)
    await get_note(session, org_id=org_id, note_id=note_id)
    tag_kind = (
        await session.execute(select(Tag.kind).where(Tag.id == tag_id))
    ).scalar_one_or_none()
    if tag_kind is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    if tag_kind is TagKind.project:
        await tag_assignment.move_to_project(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=note_id,
            project_tag_id=tag_id,
        )
    elif tag_kind is TagKind.client:
        await tag_assignment.set_client(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=note_id,
            client_tag_id=tag_id,
        )
    else:
        await tag_assignment.attach_generic(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=note_id,
            tag_id=tag_id,
        )


async def detach_tag(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    """Detach one tag. A note's CLIENT is structural: dropping it would
    leave the note with no perimeter at all, so it is refused
    (TAG_STRUCTURAL_REQUIRED); it is changed by attaching another client
    instead. The PROJECT may go -- that is the un-share path, and it
    sends the note's blobs back to the personal (NULL project)
    perimeter at once rather than at the next content edit (task
    1d152747, core/tests/test_f6b_notes.py).

    Requires an EFFECTIVE note, like its twin ``attach_tag`` already did
    (task a186c989). It is not symmetry for its own sake: dropping the
    project of a note in the bin re-scopes its indexed blobs to the
    personal perimeter, which is a retrieval-visible change made through a
    door that no read surface opens. The structural REPAIR paths
    (``taxonomy`` re-homing the survivors of a purged client) deliberately
    keep reaching trashed notes and do not come through here -- they must,
    since the DB trigger requires every note row, binned ones included, to
    carry exactly one client tag.
    """
    await require_role(session, org_id, actor_id, Role.member)
    await get_note(session, org_id=org_id, note_id=note_id)
    tag_kind = (
        await session.execute(select(Tag.kind).where(Tag.id == tag_id))
    ).scalar_one_or_none()
    if tag_kind is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    if tag_kind is TagKind.client:
        raise DomainError(MessageCode.TAG_STRUCTURAL_REQUIRED)
    if tag_kind is TagKind.project:
        await tag_assignment.clear_project(
            session, org_id=org_id, actor_id=actor_id, note_id=note_id
        )
    else:
        await tag_assignment.detach_generic(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=note_id,
            tag_id=tag_id,
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
    inside ``optimistic_update`` bypasses the ORM mapper).

    A photographer, not a read surface: it records whatever state the
    note is in, including states no reader may see. Refusing here used
    to be what stopped a mutation on an un-approved proposal -- from
    INSIDE the logger, after the write, naming the wrong reason. The
    doors do that job now, each on its own path: ``_note_set`` for the
    note itself, the ``note_parts`` chokepoints for its text,
    ``note_links._get_note`` for link and maturity writes. Structural
    junction writes that log no revision (unlinking, detaching a tag)
    are outside all three and remain ungated -- worth knowing before
    reading this as "everything is gated".
    """
    fresh = await get_note(
        session,
        org_id=org_id,
        note_id=note_id,
        include_deleted=True,
        include_proposed=True,
    )
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


# The separator ``get_body`` joins parts with, and the one the flat-body
# writer measures itself against. One constant, because the read and the
# write have to agree on it byte for byte or a read/write-back round trip
# stops being the identity.
_BODY_JOIN = "\n\n"


async def _ordered_part_bodies(session: AsyncSession, *, note_id: uuid.UUID) -> list[str]:
    """The note's part bodies in ``ord`` order: the exact list
    ``get_body`` joins. Shared with the flat-body write guard so the
    two can never disagree about how many parts there are or what the
    flat body is."""
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
    return [(b or "") for b in rows]


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
    return _BODY_JOIN.join(await _ordered_part_bodies(session, note_id=note_id))


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


# A note's markdown is unbounded, so a list endpoint that carries the
# body is O(total content of the org) in bytes, not O(rows shown).
# List endpoints carry this one-line preview instead. 220 chars is the
# cap the SPA already applied client-side back when it received the
# whole body just to slice a line out of it.
_PREVIEW_MAX_CHARS = 220
# How much of the chosen part we pull to find that line: larger than
# the cap, so a short first line still leaves something to show, and
# bounded, so N notes cost a bounded scan instead of a full read.
_PREVIEW_SCAN_CHARS = 512


async def _previews_by_note(
    session: AsyncSession, *, note_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Batched ``{note_id: one-line preview}`` for list endpoints.

    The bounded counterpart of :func:`_bodies_by_note`. ``DISTINCT ON``
    returns exactly one row per note, carrying at most
    ``_PREVIEW_SCAN_CHARS`` of its first non-empty part rather than
    every part body joined. The ordering matches ``_bodies_by_note``
    (ord, then id), so the preview is the first non-empty line the
    full body would have started with. Notes whose parts are all empty
    are absent from the map, which keeps null-vs-empty distinguished
    for the caller exactly like the body path does.
    """
    from mycelium_core.models.note_part import NotePart

    if not note_ids:
        return {}
    rows = (
        await session.execute(
            select(
                NotePart.note_id,
                func.left(NotePart.body, _PREVIEW_SCAN_CHARS),
            )
            .where(
                NotePart.note_id.in_(list(note_ids)),
                func.btrim(NotePart.body) != "",
            )
            .order_by(NotePart.note_id, NotePart.ord, NotePart.id)
            .distinct(NotePart.note_id)
        )
    ).all()
    out2: dict[uuid.UUID, str] = {}
    for nid, head in rows:
        line = next((s.strip() for s in (head or "").split("\n") if s.strip()), "")
        if line:
            out2[nid] = line[:_PREVIEW_MAX_CHARS]
    return out2


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
    include_deleted: bool = False,
    include_proposed: bool = False,
) -> int:
    # Validate existence, on the note's own perimeter. This used to admit
    # the bin for EVERY action because one of them (restore) needs to see
    # the soft-deleted row -- which left ``update_note`` rewriting the
    # title and the body of a note in the trash while ``update_part``, on
    # the very same part, answered 404. The exceptions now say so:
    # restore has to reach into the bin, and re-deleting an already
    # trashed note has to stay reachable, everything else works on a live
    # note. Flag flip + audit shared with tasks via lifecycle.transition.
    await get_note(
        session,
        org_id=org_id,
        note_id=note_id,
        include_deleted=include_deleted,
        include_proposed=include_proposed,
    )
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
    # Phase 6 final: ``transcript`` left the Note row; a restored body
    # goes back into the parts table. Other restorable fields
    # (``title``) flow through the row as before.
    transcript_target: str | None = None
    row_values: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "transcript":
            transcript_target = value or ""
        else:
            row_values[key] = value
    # ``parts`` is the structural form of the same body (migration 0089
    # onward). It is not itself a restorable FIELD -- the public
    # contract stays ``transcript``, which is what the SPA picker asks
    # for -- but when the snapshot carries it, restoring "the body"
    # means replaying the parts, so a multi-part note comes back as
    # multiple parts. Revisions written before that key existed fall
    # back to the flat path.
    snapshot: Mapping[str, Any] = revision.snapshot or {}
    parts_target: list[dict[str, Any]] | None = None
    if transcript_target is not None:
        raw_parts = snapshot.get("parts")
        if isinstance(raw_parts, list):
            parts_target = [p for p in raw_parts if isinstance(p, dict)]
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
    if parts_target is not None:
        await _restore_parts(session, org_id=org_id, note_id=note_id, parts=parts_target)
    elif transcript_target is not None:
        await _collapse_parts_to_body(
            session, org_id=org_id, note_id=note_id, body=transcript_target
        )
    return new_version


async def _restore_parts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    parts: Sequence[Mapping[str, Any]],
) -> None:
    """Replay a snapshot's ``parts`` onto a note, structure and all.

    Upsert rather than wipe-and-recreate: a part id present in both the
    snapshot and the live note is UPDATED in place, so its per-user
    collapse state (``note_part_ui_state``, FK ON DELETE CASCADE) and
    its search-blob pointer survive a restore. Live parts absent from
    the snapshot are removed with their blobs; snapshot parts absent
    from the note are re-inserted with their ORIGINAL id, so ids
    captured before the restore resolve again.

    Versions only ever go UP. A rewritten part's version is bumped from
    its CURRENT value, not reset to the snapshot's, so a client that
    read the part before the restore loses its next write instead of
    silently clobbering the restore -- ``version`` is bumped explicitly
    by the service layer here (VersionMixin has no ORM-side counter), so
    forgetting this would be a silent lost-update hole. A re-inserted
    part takes the highest version it is known to have reached, so the
    same guard holds across a trash round trip.

    Ord collisions mid-replay are fine: ``uq_note_part_note_id_ord`` is
    DEFERRABLE INITIALLY DEFERRED, so uniqueness is checked at COMMIT,
    after every row has landed on its final ord.
    """
    from mycelium_core.models.note_part import NotePart, NotePartTrash
    from mycelium_core.services import note_search

    live = {
        p.id: p
        for p in (
            (
                await session.execute(
                    select(NotePart).where(NotePart.note_id == note_id, NotePart.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    }
    wanted: set[uuid.UUID] = set()
    for entry in parts:
        try:
            pid = uuid.UUID(str(entry.get("id")))
        except (ValueError, TypeError):
            # A snapshot row without a usable id cannot be placed;
            # skipping it is better than inventing an identity.
            continue
        wanted.add(pid)
        body = entry.get("body") or ""
        ord_ = int(entry.get("ord") or 0)
        existing = live.get(pid)
        if existing is not None:
            existing.ord = ord_
            existing.title = entry.get("title")
            existing.body = body
            existing.lang = entry.get("lang")
            # Monotone: a restore is a write like any other, so anyone
            # holding the pre-restore version must lose their next save.
            existing.version = int(existing.version) + 1
        else:
            # The part may have been trashed since the snapshot. It is
            # live again now, so the trash copy is moot -- and leaving
            # it would let a later restore_part collide on the PK. Its
            # stored version is the highest this id is known to have
            # reached, so the concurrency guard survives the round trip.
            trashed_version = (
                await session.execute(
                    select(NotePartTrash.part_version).where(
                        NotePartTrash.id == pid, NotePartTrash.org_id == org_id
                    )
                )
            ).scalar_one_or_none()
            session.add(
                NotePart(
                    id=pid,
                    org_id=org_id,
                    note_id=note_id,
                    ord=ord_,
                    title=entry.get("title"),
                    body=body,
                    lang=entry.get("lang"),
                    version=max(int(entry.get("version") or 1), int(trashed_version or 1)),
                )
            )
            if trashed_version is not None:
                await session.execute(
                    delete(NotePartTrash).where(
                        NotePartTrash.id == pid, NotePartTrash.org_id == org_id
                    )
                )
        note_search.mark_note_part_dirty(session, pid)
    for pid in set(live) - wanted:
        await note_search.delete_part_index_now(session, pid)
        await session.execute(delete(NotePart).where(NotePart.id == pid, NotePart.org_id == org_id))
    await session.flush()


async def _collapse_parts_to_body(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
    body: str,
) -> None:
    """Make ``body`` the note's ENTIRE body: part(ord=0) becomes it and
    every other part goes.

    The fallback for revisions written before snapshots carried
    ``parts``. Those snapshots hold only the flat join of every part,
    so structure is genuinely unrecoverable from them -- but the join
    already CONTAINS the other parts' text. Writing it into part 0 and
    leaving the rest in place (what the restore used to do) duplicated
    every part after the first into the note body.
    """
    from mycelium_core.models.note_part import NotePart
    from mycelium_core.services import note_search

    stale = (
        (
            await session.execute(
                select(NotePart.id).where(
                    NotePart.note_id == note_id,
                    NotePart.org_id == org_id,
                    NotePart.ord != 0,
                )
            )
        )
        .scalars()
        .all()
    )
    for pid in stale:
        await note_search.delete_part_index_now(session, pid)
        await session.execute(delete(NotePart).where(NotePart.id == pid, NotePart.org_id == org_id))
    await _upsert_part_zero(session, org_id=org_id, note_id=note_id, body=body)


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
    # ``text`` is the FLAT body: the very string ``get_body`` returns, which
    # is the ``\n\n`` join of EVERY part. The join is not invertible -- a
    # blank line inside a part reads the same as a part boundary -- so this
    # writer cannot express a multi-part note. It used to try anyway, writing
    # the whole join into part 0 and leaving parts 1..N alive, which turned
    # the ordinary read/modify/write-back of an MCP or CLI caller into silent
    # duplication ('AAA\n\nBBB' came back as 'AAA\n\nBBB\n\nBBB'). The same
    # bug is already written up for the restore path in
    # ``_collapse_parts_to_body``, where it was fixed only locally.
    #
    # Collapsing instead of duplicating is NOT the fix: deleting parts 1..N
    # cascades ``annotation.note_part_id`` (ON DELETE CASCADE), so a flat body
    # write would silently destroy every comment and suggestion anchored to
    # those parts, plus their per-user collapse state and search blobs.
    #
    # So: unchanged flat body is a no-op on the parts (which is what makes a
    # read/write-back round trip the identity), and a CHANGED flat body on a
    # multi-part note is refused. The structured writers
    # (``note_parts.update_part`` and friends, exposed over API, MCP and CLI)
    # are the ones that can say which part changed.
    skip_body_write = False
    if text is not None:
        bodies = await _ordered_part_bodies(session, note_id=note_id)
        if len(bodies) > 1:
            if text != _BODY_JOIN.join(bodies):
                raise UnprocessableError(MessageCode.NOTE_BODY_MULTIPART, parts=len(bodies))
            skip_body_write = True
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
    if text is not None and not skip_body_write:
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
    separator doesn't get a second copy. This keeps appended paragraphs
    aligned with the existing block structure without introducing
    accidental gaps.

    The previous shape tested ``base.rstrip(" \\t\\n\\r").endswith(
    separator.rstrip(" \\t\\n\\r"))``. For every whitespace separator the
    right-hand side is ``""``, so the test was vacuously true and the branch
    always fired. Two bugs came out of it:

    - with ``separator="\\n\\n"`` it ran ``base.rstrip()``, which eats a
      two-space hard break off the end of the stored body on EVERY append;
    - with any other whitespace separator it returned ``base + text``, i.e.
      dropped the separator entirely (``_collapsed_concat("a", "\\n", "b")``
      was ``"ab"``). ``separator`` is an MCP tool parameter, so that one was
      reachable from an agent.

    The blank-line separator is the only one with a collapse rule, and it is
    now stated in newlines rather than in ``strip``: pad the trailing newline
    run up to two, never remove bytes. Whitespace on the last CONTENT line is
    markdown (a two-space hard break), not stray formatting, and normalising
    it is exactly the class of damage the byte-exactness work exists to stop.
    """
    base = current or ""
    if not base:
        return text
    if base.endswith(separator):
        return base + text
    if separator == "\n\n":
        # 0 or 1 here: a run of 2+ already matched ``endswith`` above.
        trailing_newlines = len(base) - len(base.rstrip("\n"))
        return base + "\n" * (2 - trailing_newlines) + text
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


async def protect_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
    protected: bool = True,
) -> int:
    """Fase P (task 561c6aca): mark finished prose the distiller must never
    compact (or release it with ``protected=False``). The flag gates
    ``is_inert`` and every distillation surface; the user has the last word."""
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values={"protected": protected},
        action="protect",
    )


async def soft_delete_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
) -> int:
    """Move a note to the bin. IDEMPOTENT: deleting one that is already
    there returns its current version and changes nothing.

    Not a nicety. ``deleted_at`` is not just a flag, it is the retention
    clock: the autonomous sweep purges what has been in the bin longer
    than the window, so re-stamping it on every call meant a retried
    delete -- the ordinary reaction of an agent to a timeout -- pushed the
    note's purge date forward, indefinitely, one retry at a time. It also
    bumped the version and wrote a second ``_delete`` revision saying
    nothing had changed.

    ``expected_version`` is checked BEFORE the short-circuit, mirroring
    ``garden_review.reject_node``: a caller working from a stale read
    learns it is stale instead of being told its delete landed.
    """
    note = await get_note(session, org_id=org_id, note_id=note_id, include_deleted=True)
    if note.deleted_at is not None:
        if note.version != expected_version:
            raise ConflictError(MessageCode.CONFLICT_STALE_VERSION)
        return note.version
    return await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values={"deleted_at": dt.datetime.now(tz=dt.UTC)},
        action="delete",
        # Reachable for a note already in the bin so the branch above can
        # answer; the write itself only ever runs on a live one.
        include_deleted=True,
    )


async def restore_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    expected_version: int,
) -> int:
    # The only action that must see the soft-deleted row -- and the only one
    # that may touch a proposal, but ONLY a rejected one:
    # ``garden_review.reject_node`` rejects by soft-deleting and leaves
    # ``review_state='proposed'``, promising the note is "reversible via the
    # normal restore path". Without the opt-in that promise was false. Passing
    # it unconditionally would be worse than the bug: with both legs relaxed
    # the guard degenerates to "the row exists", and a LIVE proposal -- one
    # still waiting in the review inbox, which no read surface will open --
    # would take a version bump and a revision from anyone with notes:write.
    # So the un-reject is opened by hand, on the one state that means it.
    note = await get_note(
        session,
        org_id=org_id,
        note_id=note_id,
        include_deleted=True,
        include_proposed=True,
    )
    rejected = note.review_state == "proposed" and note.deleted_at is not None
    if note.review_state == "proposed" and not rejected:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    new_version = await _note_set(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        expected_version=expected_version,
        values={"deleted_at": None},
        action="restore",
        include_deleted=True,
        include_proposed=rejected,
    )
    if rejected:
        # The undo has to leave a mark of its own, or the per-model
        # reliability signal keeps counting a rejection the human took back
        # (and, after a second look that approves, counts the node twice).
        # Local import: ``garden_review`` owns the review vocabulary and
        # imports this module.
        from mycelium_core.services import garden_review as garden_review_svc

        note.version = new_version
        await garden_review_svc.record_unreject(
            session, org_id=org_id, actor_id=actor_id, note=note
        )
    return new_version


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
    # Every note belongs to exactly one client and to AT MOST one
    # project, both carried in ``note_tags`` (migration 0016, ADR-0007
    # single-source-of-truth, mirrors task_tags). tag_assignment owns
    # that pair: it checks ``project_id`` really is a project tag and
    # derives the client from it, so a client id passed here is now
    # refused instead of being inserted as the project and paired with
    # the "Personal" fallback -- which left the note with two clients.
    # No project means the personal perimeter (docs/adr/0021), not a
    # defect: the project stays NULL and the default client applies.
    structural = await tag_assignment.resolve_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        project_tag_id=project_id,
    )
    await tag_assignment.set_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note.id,
        structural=structural,
        on_create=True,
    )
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


async def _project_tag_for_task(session: AsyncSession, *, task_id: uuid.UUID) -> uuid.UUID | None:
    """The task's project tag id. A task carries exactly one project
    (docs/adr/0003, owned by ``services.tag_assignment``), so this is a
    single-row read; a second row is corrupted taxonomy and is surfaced
    rather than narrowed by an unordered LIMIT 1 that would drop the
    work note into an arbitrary project. ``None`` only for legacy rows
    predating the invariant."""
    rows = (
        (
            await session.execute(
                select(Tag.id)
                .join(TaskTag, TaskTag.tag_id == Tag.id)
                .where(TaskTag.task_id == task_id, Tag.kind == TagKind.project)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > 1:
        raise DomainError(MessageCode.TAG_MULTIPLE_PROJECTS)
    return rows[0] if rows else None


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
    is created via ``create_note`` so the client/project enforcement
    runs; it inherits the task's project (hence its client). The task's
    free-form tags are then propagated onto the note, and its structural
    pair adopted, so filters and /focus see both on the same axes."""
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
                select(Note).where(Note.id.in_(note_ids), effective_note_clause())
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    # The work note is born under the task's project (hence its client)
    # instead of under the Personal fallback, so its retrieval perimeter
    # is right from the first index pass, with no move afterwards.
    project_id = await _project_tag_for_task(session, task_id=task_id)
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
    ``create_note`` so the client/project enforcement runs, inheriting
    the task's project (hence client); title defaults to the task
    title. ``notes.task_id`` is set so time logged in the note rolls up
    to this task."""
    task = (
        await session.execute(select(Task).where(Task.id == task_id, Task.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    project_id = await _project_tag_for_task(session, task_id=task_id)
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
    """Give ``note_id`` the tags of ``task_id``: the free-form facets
    are added to whatever the note already has, while the task's
    structural pair REPLACES the note's own.

    The pair is set, never appended. The note already carries a client
    from ``create_note`` (the task's project's, or the "Personal"
    fallback when the task has no project), so copying the task's client
    additively gave the note TWO client tags whenever the two disagreed
    -- precisely what docs/adr/0003 forbids. ``tag_assignment``
    adjudicates the pair (the project decides the client), re-scopes the
    blobs if the project moved and writes the audit rows, so there are
    none to write here."""
    rows = (
        await session.execute(
            select(Tag.id, Tag.kind)
            .join(TaskTag, TaskTag.tag_id == Tag.id)
            .where(TaskTag.task_id == task_id)
        )
    ).all()
    structural_ids: list[uuid.UUID] = []
    freeform_ids: list[uuid.UUID] = []
    for tag_id, kind in rows:
        target = structural_ids if kind in (TagKind.client, TagKind.project) else freeform_ids
        target.append(tag_id)
    if structural_ids:
        structural = await tag_assignment.resolve_structural(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            requested=structural_ids,
        )
        await tag_assignment.set_structural(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=note_id,
            structural=structural,
            # Both callers run this immediately after creating the note,
            # so the inherited pair is genesis, not a working touch.
            on_create=True,
        )
    for tag_id in freeform_ids:
        await tag_assignment.attach_generic(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=note_id,
            tag_id=tag_id,
            on_create=True,
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
    ``limit`` + the ``after`` keyset cursor page a long transcript.

    Empty unless the note is EFFECTIVE (task a186c989). ``note_turns`` is
    the second child table that holds a note's text -- the transcript of a
    conversation note -- so leaving it note-blind would have kept the door
    the parts gate just closed wide open one table over.
    """
    stmt = (
        select(NoteTurn)
        .join(Note, Note.id == NoteTurn.note_id)
        .where(
            NoteTurn.note_id == note_id,
            NoteTurn.org_id == org_id,
            effective_note_clause(),
        )
    )
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
    must go so an erase leaves no embedding behind.

    Trashed parts (``note_part_trash``, migration 0089) hold bodies too;
    they go with the note row via the FK CASCADE, and their blobs were
    already dropped when they were trashed -- so an erase leaves nothing
    of them behind either."""
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
    # Erase any knowledge-graph facts extracted from this note BEFORE the note
    # row goes (the FK is ON DELETE SET NULL, which would otherwise sever the
    # provenance handle and strand the facts). GDPR right-to-erasure overrides
    # invalidate-not-delete -- this is the only sanctioned KG hard-delete.
    from mycelium_core.services import kg as kg_svc

    await kg_svc.erase_by_source(session, org_id=org_id, actor_id=actor_id, source_note_id=note_id)
    # The recovery history holds FULL COPIES of the body (``transcript``,
    # plus the per-part ``parts`` since migration 0089), and it is NOT
    # reached by a foreign key -- ``entity_revision`` is polymorphic on
    # ``(entity_kind, entity_id)``. What clears it is the AFTER DELETE
    # trigger ``trg_note_revision_cascade`` (migration 0006), which fires
    # on the row delete below. No explicit sweep here: a second delete
    # would be dead code whose only effect is to suggest the trigger
    # cannot be relied on.
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
