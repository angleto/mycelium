"""Note garden ecosystem operations (docs/adr/0029, P1).

Six named operations + maturity setter:

- ``set_maturity(note_id, maturity)``: manual override of the garden
  lifecycle. Auto-transitions (touched-recently / untouched-long /
  on-touch) live in a worker tick (see ``worker/garden.py``); this
  is the always-allowed manual setter.

- ``link_notes(parent, child, kind)`` / ``unlink_notes(...)``: typed
  M:N between notes, the mycelial 4-verb model (ADR-0040): ``hypha_of``
  (derived from, directional), ``related`` (UNDIRECTED association,
  canonicalised to parent < child here), ``supersedes`` / ``contradicts``
  (directional; both decay the target toward ``dormant`` on creation,
  feeding the deadwood -> humus cycle).

- ``derive_task_from_note(note_id, title, ...)``: the note remains
  alive; a new task is created with a ``derived_from`` link. Use
  case: "the note made me realise I should do X".

- ``promote_note_to_task(note_id, title?)``: the note is transplanted
  (``promoted_at = now``, becomes read-only at the service layer);
  a new task is created with a ``promoted_from`` link.

- ``start_task_on_note(note_id, task_id)``: link an existing task to
  an existing note with ``subject`` semantics. The task is the work
  of growing the note.

- ``record_task_artifact(task_id, note_id)``: link with ``artifact``
  semantics. The closing task produced (or updated) this note.

Every operation is owner / member gated, audited via ``audit.log``,
and idempotent on the link tables (the UNIQUE constraint deduplicates
re-applications).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.identity import Identity
from mycelium_core.models.membership import Role
from mycelium_core.models.note import Note, NoteMaturity
from mycelium_core.models.note_link import (
    NOTE_NOTE_LINK_KILLING_KINDS,
    NOTE_NOTE_LINK_KINDS,
    NOTE_NOTE_LINK_UNDIRECTED_KINDS,
    NOTE_TASK_LINK_KINDS,
    NoteNoteLink,
    NoteTaskLink,
)
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.models.task import Task
from mycelium_core.services import audit, note_inert, tag_assignment
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.note_effective import effective_note_clause
from mycelium_core.services.rbac import require_role

_VALID_MATURITY: frozenset[str] = frozenset(m.value for m in NoteMaturity)


async def _get_note(session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID) -> Note:
    """The write-side guard of this module (link, unlink-adjacent flows,
    set_maturity, promote/derive/start/record): the note must be
    EFFECTIVE (task a186c989).

    The ``deleted_at`` leg was here from the start; the ADR-0043 leg was
    not, so ``promote_note_to_task`` -- which copies the body into
    ``Task.description``, and from there into an agent's prompt -- was
    the one mutation that still succeeded on an un-approved proposal.
    The org filter stays explicit even though the shared clause does not
    carry it: the FK alone does not enforce an org match.
    """
    row = (
        await session.execute(
            select(Note).where(
                Note.id == note_id,
                Note.org_id == org_id,
                effective_note_clause(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # A note, not a memory blob: the old MEMORY_NOT_FOUND named the
        # wrong entity, which now also reads wrong for a gated proposal.
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    return row


async def _note_project_tag_ids(session: AsyncSession, *, note_id: uuid.UUID) -> list[uuid.UUID]:
    """The project tag(s) the note carries. A READ: every structural
    junction WRITE goes through tag_assignment (docs/adr/0003).

    Returns a list rather than "the" project so the cardinality rule
    stays in ONE place -- ``resolve_structural`` refuses a note carrying
    two projects (TAG_MULTIPLE_PROJECTS) instead of a local tie-break
    silently picking one. The note's CLIENT is deliberately not read:
    the project is the truth and carries its own client, and a task
    cannot honour a client alone (it must have a project, so a
    projectless note yields the default General/Personal pair).
    """
    return list(
        (
            await session.execute(
                select(NoteTag.tag_id)
                .join(Tag, Tag.id == NoteTag.tag_id)
                .where(NoteTag.note_id == note_id, Tag.kind == TagKind.project)
            )
        )
        .scalars()
        .all()
    )


def _resolved_tag_ids(structural: tag_assignment.Structural) -> list[uuid.UUID]:
    """The resolved pair + the free-form facets, as the flat list
    ``tasks_svc.create_task`` takes. ``project_tag_id`` is never None for
    a task (no orphan tasks); the arm exists because the dataclass also
    models notes."""
    ids = [structural.client_tag_id]
    if structural.project_tag_id is not None:
        ids.append(structural.project_tag_id)
    return [*ids, *structural.generic_ids]


async def _actor_identity_id(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> uuid.UUID | None:
    """Resolve the caller's Identity row in this org. Returns None if
    the user has no Identity in the workspace (shouldn't happen for
    interactive flows but the field is nullable at the DB to support
    backfill, so we tolerate the absence)."""
    return (
        await session.execute(
            select(Identity.id).where(
                Identity.org_id == org_id,
                Identity.user_id == actor_id,
            )
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Maturity setter
# ---------------------------------------------------------------------------


async def set_maturity(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    maturity: str,
) -> Note:
    """Manual override of the garden lifecycle. Always wins over the
    automatic worker tick. Audited.

    Cannot set ``maturity`` on a note that was already transplanted
    (``promoted_at IS NOT NULL``): a transplanted plant is read-only.
    """
    if maturity not in _VALID_MATURITY:
        raise DomainError(
            MessageCode.NOTE_MATURITY_INVALID,
            maturity=maturity,
            valid=", ".join(sorted(_VALID_MATURITY)),
        )
    await require_role(session, org_id, actor_id, Role.member)
    note = await _get_note(session, org_id=org_id, note_id=note_id)
    if note.promoted_at is not None:
        raise DomainError(MessageCode.NOTE_PROMOTED_READONLY)
    if note.maturity == maturity:
        return note
    prior = note.maturity
    note.maturity = maturity
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        action="set_maturity",
        diff={"from": prior, "to": maturity},
    )
    return note


# ---------------------------------------------------------------------------
# Note <-> Note links
# ---------------------------------------------------------------------------


async def link_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    parent_note_id: uuid.UUID,
    child_note_id: uuid.UUID,
    kind: str,
) -> NoteNoteLink:
    if kind not in NOTE_NOTE_LINK_KINDS:
        raise DomainError(
            MessageCode.NOTE_LINK_KIND_INVALID,
            kind=kind,
            valid=", ".join(sorted(NOTE_NOTE_LINK_KINDS)),
        )
    if parent_note_id == child_note_id:
        raise DomainError(MessageCode.NOTE_LINK_SELF)
    # Undirected kinds (``related``) have unordered endpoints:
    # canonicalise to parent < child by id string so (a, b) and (b, a)
    # collapse to one edge under the UNIQUE constraint.
    if kind in NOTE_NOTE_LINK_UNDIRECTED_KINDS and str(parent_note_id) > str(child_note_id):
        parent_note_id, child_note_id = child_note_id, parent_note_id
    await require_role(session, org_id, actor_id, Role.member)
    # Both notes must exist in this org (FK alone does not enforce
    # org match; defence in depth).
    await _get_note(session, org_id=org_id, note_id=parent_note_id)
    child_note = await _get_note(session, org_id=org_id, note_id=child_note_id)

    existing = (
        await session.execute(
            select(NoteNoteLink).where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.parent_note_id == parent_note_id,
                NoteNoteLink.child_note_id == child_note_id,
                NoteNoteLink.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    identity_id = await _actor_identity_id(session, org_id=org_id, actor_id=actor_id)
    link = NoteNoteLink(
        org_id=org_id,
        parent_note_id=parent_note_id,
        child_note_id=child_note_id,
        kind=kind,
        created_by=identity_id,
    )
    session.add(link)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        # Lost a race against a concurrent identical insert (same
        # unique tuple): fetch and return the winner.
        return (
            await session.execute(
                select(NoteNoteLink).where(
                    NoteNoteLink.org_id == org_id,
                    NoteNoteLink.parent_note_id == parent_note_id,
                    NoteNoteLink.child_note_id == child_note_id,
                    NoteNoteLink.kind == kind,
                )
            )
        ).scalar_one()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_note_link",
        entity_id=link.id,
        action="create",
        diff={
            "parent_note_id": str(parent_note_id),
            "child_note_id": str(child_note_id),
            "kind": kind,
        },
    )
    # ``supersedes`` / ``contradicts`` decay the target idea toward
    # ``dormant``: a superseded or refuted idea rots into the deadwood ->
    # humus cycle. One-way nudge (removing the link does not revive it),
    # manual maturity still overrides, and a transplanted (promoted) note
    # is left read-only.
    if (
        kind in NOTE_NOTE_LINK_KILLING_KINDS
        and child_note.promoted_at is None
        and child_note.maturity != NoteMaturity.dormant.value
        # Invariant (task 8a26c000): do not dormant a live child (one with
        # an open linked task), even when superseded/contradicted.
        and not await note_inert.note_has_open_work(session, note_id=child_note_id)
    ):
        prior = child_note.maturity
        child_note.maturity = NoteMaturity.dormant.value
        await session.flush()
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note",
            entity_id=child_note_id,
            action="auto_dormant",
            diff={"from": prior, "to": NoteMaturity.dormant.value, "cause": kind},
        )
    return link


async def unlink_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    parent_note_id: uuid.UUID,
    child_note_id: uuid.UUID,
    kind: str,
) -> bool:
    await require_role(session, org_id, actor_id, Role.member)
    # Match the canonical orientation used on creation for undirected
    # kinds, so unlinking (b, a) finds the row stored as (a, b).
    if kind in NOTE_NOTE_LINK_UNDIRECTED_KINDS and str(parent_note_id) > str(child_note_id):
        parent_note_id, child_note_id = child_note_id, parent_note_id
    row = (
        await session.execute(
            select(NoteNoteLink).where(
                NoteNoteLink.org_id == org_id,
                NoteNoteLink.parent_note_id == parent_note_id,
                NoteNoteLink.child_note_id == child_note_id,
                NoteNoteLink.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_note_link",
        entity_id=row.id,
        action="delete",
    )
    return True


async def list_workspace_note_links(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
) -> list[NoteNoteLink]:
    """Return every note-to-note link in the workspace. Used by the
    garden mindmap to render the full edge set in one round-trip
    instead of N per-note queries (docs/adr/0029 D6: workspace-scoped
    notes are few-per-tenant, so a single query is preferable to
    server-side aggregation)."""
    return list(
        (await session.execute(select(NoteNoteLink).where(NoteNoteLink.org_id == org_id)))
        .scalars()
        .all()
    )


async def list_note_links(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
) -> tuple[list[NoteNoteLink], list[NoteNoteLink]]:
    """Return ``(outgoing, incoming)``: links where this note is the
    parent (outgoing) and where it is the child (incoming / backlinks)."""
    outgoing = list(
        (
            await session.execute(
                select(NoteNoteLink).where(
                    NoteNoteLink.org_id == org_id,
                    NoteNoteLink.parent_note_id == note_id,
                )
            )
        )
        .scalars()
        .all()
    )
    incoming = list(
        (
            await session.execute(
                select(NoteNoteLink).where(
                    NoteNoteLink.org_id == org_id,
                    NoteNoteLink.child_note_id == note_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return outgoing, incoming


# ---------------------------------------------------------------------------
# Note <-> Task links (raw)
# ---------------------------------------------------------------------------


async def _link_note_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    task_id: uuid.UUID,
    kind: str,
) -> NoteTaskLink:
    if kind not in NOTE_TASK_LINK_KINDS:
        raise DomainError(
            MessageCode.NOTE_TASK_LINK_KIND_INVALID,
            kind=kind,
            valid=", ".join(sorted(NOTE_TASK_LINK_KINDS)),
        )
    existing = (
        await session.execute(
            select(NoteTaskLink).where(
                NoteTaskLink.org_id == org_id,
                NoteTaskLink.note_id == note_id,
                NoteTaskLink.task_id == task_id,
                NoteTaskLink.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    identity_id = await _actor_identity_id(session, org_id=org_id, actor_id=actor_id)
    link = NoteTaskLink(
        org_id=org_id,
        note_id=note_id,
        task_id=task_id,
        kind=kind,
        created_by=identity_id,
    )
    session.add(link)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return (
            await session.execute(
                select(NoteTaskLink).where(
                    NoteTaskLink.org_id == org_id,
                    NoteTaskLink.note_id == note_id,
                    NoteTaskLink.task_id == task_id,
                    NoteTaskLink.kind == kind,
                )
            )
        ).scalar_one()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_task_link",
        entity_id=link.id,
        action="create",
        diff={
            "note_id": str(note_id),
            "task_id": str(task_id),
            "kind": kind,
        },
    )
    return link


# Priority order used by ``primary_task_id_for_note`` to pick the
# canonical task for a note when more than one kind links them
# (e.g. a note that is both ``subject`` of an ongoing task and
# ``artifact`` of a closed previous one). Mirrors Proposal A's
# original intent: the active work link wins; artifact is the
# historical record; the rest are weaker pointers.
_TASK_LINK_PRIORITY: tuple[str, ...] = (
    "subject",
    "artifact",
    "promoted_from",
    "derived_from",
)


async def primary_task_id_for_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID,
) -> uuid.UUID | None:
    """Resolve the canonical task for a note across the four typed
    relations (docs/adr/0029 P3 replaces ``note.task_id``). Priority:
    subject > artifact > promoted_from > derived_from. Returns the
    earliest match's ``task_id`` or None.
    """
    raw = list(
        (
            await session.execute(
                select(NoteTaskLink.task_id, NoteTaskLink.kind, NoteTaskLink.created_at).where(
                    NoteTaskLink.org_id == org_id,
                    NoteTaskLink.note_id == note_id,
                )
            )
        ).all()
    )
    if not raw:
        return None
    raw.sort(
        key=lambda r: (
            _TASK_LINK_PRIORITY.index(r[1]) if r[1] in _TASK_LINK_PRIORITY else 999,
            r[2],
        )
    )
    return uuid.UUID(str(raw[0][0]))


async def primary_task_ids_for_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_ids: list[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """Batch counterpart used by list endpoints to avoid N+1: returns
    a ``{note_id: primary_task_id}`` mapping, applying the same
    priority as ``primary_task_id_for_note``."""
    if not note_ids:
        return {}
    rows = (
        await session.execute(
            select(
                NoteTaskLink.note_id,
                NoteTaskLink.task_id,
                NoteTaskLink.kind,
                NoteTaskLink.created_at,
            ).where(
                NoteTaskLink.org_id == org_id,
                NoteTaskLink.note_id.in_(note_ids),
            )
        )
    ).all()
    bucket: dict[uuid.UUID, list[tuple[str, object, uuid.UUID]]] = {}
    for nid, tid, kind, created_at in rows:
        bucket.setdefault(nid, []).append((kind, created_at, tid))
    out: dict[uuid.UUID, uuid.UUID] = {}
    for nid, candidates in bucket.items():
        candidates.sort(
            key=lambda c: (
                _TASK_LINK_PRIORITY.index(c[0]) if c[0] in _TASK_LINK_PRIORITY else 999,
                c[1],
            )
        )
        out[nid] = uuid.UUID(str(candidates[0][2]))
    return out


async def task_titles_for_ids(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_ids: list[uuid.UUID],
) -> dict[uuid.UUID, str | None]:
    """Batch ``{task_id: title}`` for the given task ids. Includes
    archived and soft-deleted tasks on purpose: a note's "work note"
    banner shows the linked task's title regardless of the task's
    lifecycle state, so it must not blank out for a note linked to a
    closed task. Duplicate ids in the input collapse; missing ids are
    simply absent from the result."""
    uniq = list({tid for tid in task_ids if tid is not None})
    if not uniq:
        return {}
    rows = (
        await session.execute(
            select(Task.id, Task.title).where(
                Task.org_id == org_id,
                Task.id.in_(uniq),
            )
        )
    ).all()
    return {tid: title for tid, title in rows}


async def derived_task_ids_for_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Batched ``{note_id: [task_id, ...]}`` for the tasks generated
    from each note (link kinds ``derived_from`` and ``promoted_from``).
    Powers the "N tasks" chip in the note list (Punto 4 follow-up):
    one task is the canonical "primary"; the rest are extra fruits.
    Sorted by ``created_at`` so the list reflects emission order.
    Returns an empty mapping for notes with no generated task."""
    if not note_ids:
        return {}
    rows = (
        await session.execute(
            select(
                NoteTaskLink.note_id,
                NoteTaskLink.task_id,
                NoteTaskLink.created_at,
            )
            .where(
                NoteTaskLink.org_id == org_id,
                NoteTaskLink.note_id.in_(note_ids),
                NoteTaskLink.kind.in_(("derived_from", "promoted_from")),
            )
            .order_by(NoteTaskLink.created_at.asc())
        )
    ).all()
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for nid, tid, _ in rows:
        out.setdefault(nid, []).append(tid)
    return out


async def linked_task_counts_for_notes(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Batched ``{note_id: count}`` of ALL task links to each note
    (every kind: derived_from, promoted_from, subject, artifact).

    Task 1e07437e: the SPA's "N tasks" chip on NoteListItem used to
    count only the two "fruit" kinds (derived_from + promoted_from).
    A note that was only linked as ``subject`` or ``artifact`` showed
    no chip even though the drawer panel listed real links. This
    helper returns the full count so the chip matches the drawer.

    One batched ``GROUP BY`` per call (O(notes), not O(notes*tasks)).
    Notes with zero links are omitted from the mapping.
    """
    if not note_ids:
        return {}
    rows = (
        await session.execute(
            select(NoteTaskLink.note_id, func.count(NoteTaskLink.id))
            .where(
                NoteTaskLink.org_id == org_id,
                NoteTaskLink.note_id.in_(note_ids),
            )
            .group_by(NoteTaskLink.note_id)
        )
    ).all()
    return {nid: int(c) for nid, c in rows}


async def notes_for_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    kinds: tuple[str, ...] | None = None,
    include_deleted: bool = False,
) -> list[uuid.UUID]:
    """The EFFECTIVE note ids linked to the given task. Optional ``kinds``
    filter restricts to specific relation types; default returns every kind.
    Replaces the legacy ``WHERE Note.task_id = task_id`` pattern.

    The perimeter is here, not at the call sites (task 854f1c28). This used
    to return raw junction rows and leave the filtering to whoever resolved
    them: four of its five callers then wrote ``deleted_at IS NULL`` and
    forgot the ADR-0043 leg, so the body of an un-approved proposal reached
    an agent's prompt through the one path neither the retrieval predicate
    nor the note/graph predicate covers. A link row is not a claim that the
    note is visible, so the default answer is the effective set.

    ``include_deleted`` is the trash-view opt-in (the MCP ``list_notes``
    ``task_id`` path offers it): like the shared clause, it drops ONLY the
    soft-delete leg -- an un-approved proposal is never returned.
    """
    stmt = (
        select(NoteTaskLink.note_id)
        .join(Note, Note.id == NoteTaskLink.note_id)
        .where(
            NoteTaskLink.org_id == org_id,
            NoteTaskLink.task_id == task_id,
            effective_note_clause(include_deleted=include_deleted),
        )
    )
    if kinds:
        stmt = stmt.where(NoteTaskLink.kind.in_(kinds))
    return list((await session.execute(stmt)).scalars().all())


async def ensure_artifact_link(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    task_id: uuid.UUID,
) -> NoteTaskLink:
    """Idempotent: writes a ``kind='artifact'`` link between note and
    task. Used by the Proposal-A migration shims (``create_note_for_task``,
    coordination handoff write-back) to replace the legacy
    ``note.task_id`` setter."""
    return await _link_note_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        task_id=task_id,
        kind="artifact",
    )


async def clear_artifact_links(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
) -> int:
    """Remove every ``kind='artifact'`` link for the given note.
    Replaces ``note.task_id = None`` (clear). Returns the count
    removed; emits one ``unlink`` audit entry per row removed."""
    rows = list(
        (
            await session.execute(
                select(NoteTaskLink).where(
                    NoteTaskLink.org_id == org_id,
                    NoteTaskLink.note_id == note_id,
                    NoteTaskLink.kind == "artifact",
                )
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        await session.delete(r)
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="note_task_link",
            entity_id=r.id,
            action="delete",
        )
    if rows:
        await session.flush()
    return len(rows)


async def list_note_task_links(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    note_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
) -> list[NoteTaskLink]:
    """One of ``note_id`` / ``task_id`` must be set. Returns the
    typed links touching that anchor."""
    if note_id is None and task_id is None:
        raise DomainError(MessageCode.NOTE_TASK_LINK_ANCHOR_REQUIRED)
    stmt = select(NoteTaskLink).where(NoteTaskLink.org_id == org_id)
    if note_id is not None:
        stmt = stmt.where(NoteTaskLink.note_id == note_id)
    if task_id is not None:
        stmt = stmt.where(NoteTaskLink.task_id == task_id)
    return list((await session.execute(stmt)).scalars().all())


async def unlink_note_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    task_id: uuid.UUID,
    kind: str,
) -> bool:
    """Remove a single typed note↔task link. Symmetric to ``unlink_notes``
    on the note-to-note side. Idempotent: returns False if no row matched.
    Promoted notes are read-only at the service layer, so removing a
    ``promoted_from`` link is refused (the original promotion is the only
    way to mark a note transplanted, and unlinking it would orphan the
    ``promoted_at`` timestamp)."""
    if kind not in NOTE_TASK_LINK_KINDS:
        raise DomainError(
            MessageCode.NOTE_TASK_LINK_KIND_INVALID,
            kind=kind,
            valid=", ".join(sorted(NOTE_TASK_LINK_KINDS)),
        )
    await require_role(session, org_id, actor_id, Role.member)
    row = (
        await session.execute(
            select(NoteTaskLink).where(
                NoteTaskLink.org_id == org_id,
                NoteTaskLink.note_id == note_id,
                NoteTaskLink.task_id == task_id,
                NoteTaskLink.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if kind == "promoted_from":
        # The promotion side-effect (note.promoted_at) is set in
        # ``promote_note_to_task`` and there is no symmetric unmake.
        raise DomainError(MessageCode.NOTE_TASK_LINK_PROMOTED_IMMUTABLE)
    await session.delete(row)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note_task_link",
        entity_id=row.id,
        action="delete",
        diff={
            "note_id": str(note_id),
            "task_id": str(task_id),
            "kind": kind,
        },
    )
    return True


# ---------------------------------------------------------------------------
# Named lifecycle operations
# ---------------------------------------------------------------------------


async def derive_task_from_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    title: str,
    description: str | None = None,
    estimate_effort_h: Decimal | None = None,
    extra_tag_ids: list[uuid.UUID] | None = None,
) -> tuple[Task, NoteTaskLink]:
    """A new task falls out of the note as a fruit; the note stays
    alive. It inherits the note's project -- hence, through
    ``project_profile.client_tag_id``, the note's client. A projectless
    note (the personal perimeter, docs/adr/0021) has no project to pass
    on, so the task falls back to the default General / Personal pair:
    a task must have a project, so the note's client cannot ride alone.

    ``extra_tag_ids`` goes through the same resolution, so a caller
    naming a second project, or a client the inherited project
    contradicts, is refused rather than silently attached alongside."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await _get_note(session, org_id=org_id, note_id=note_id)
    structural = await tag_assignment.resolve_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        requested=[
            *await _note_project_tag_ids(session, note_id=note.id),
            *(extra_tag_ids or ()),
        ],
    )
    task = await tasks_svc.create_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        title=title,
        description=description,
        estimate_effort_h=estimate_effort_h,
        tag_ids=_resolved_tag_ids(structural),
    )
    link = await _link_note_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note.id,
        task_id=task.id,
        kind="derived_from",
    )
    return task, link


async def promote_note_to_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note_id: uuid.UUID,
    title: str | None = None,
) -> tuple[Task, NoteTaskLink]:
    """Transplant the note: a new task carries the note as substrate;
    the note is marked read-only (``promoted_at = now``). The task
    inherits the note's project (hence its client), exactly like
    :func:`derive_task_from_note`; a projectless note yields the default
    General / Personal pair.

    The title defaults to the note's title (or a derived snippet of
    the transcript when the title is empty).
    """
    await require_role(session, org_id, actor_id, Role.member)
    note = await _get_note(session, org_id=org_id, note_id=note_id)
    if note.promoted_at is not None:
        raise DomainError(MessageCode.NOTE_PROMOTED_READONLY)
    chosen_title = (title or note.title or "").strip()
    # Phase 6 final: read the body from note_part(ord=0)+ rather than
    # the dropped ``transcript`` column. ``get_body`` joins every
    # part by blank line.
    from mycelium_core.services.notes import get_body as _get_body

    body_text = await _get_body(session, note_id=note_id)
    if not chosen_title:
        # Derive from the first non-empty line of the body.
        lines = body_text.strip().splitlines()
        chosen_title = lines[0][:120] if lines else "promoted note"
    structural = await tag_assignment.resolve_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        requested=await _note_project_tag_ids(session, note_id=note.id),
    )
    task = await tasks_svc.create_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        title=chosen_title,
        description=note.summary or body_text,
        tag_ids=_resolved_tag_ids(structural),
    )
    link = await _link_note_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note.id,
        task_id=task.id,
        kind="promoted_from",
    )
    note.promoted_at = dt.datetime.now(dt.UTC)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note.id,
        action="promote",
        diff={"task_id": str(task.id)},
    )
    return task, link


async def start_task_on_note(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    note_id: uuid.UUID,
) -> NoteTaskLink:
    """Watering: the task is the work of growing the note. Same-org
    check on both ends (defence in depth: the FK alone is
    org-agnostic)."""
    await require_role(session, org_id, actor_id, Role.member)
    await _get_note(session, org_id=org_id, note_id=note_id)
    task = (
        await session.execute(select(Task).where(Task.id == task_id, Task.org_id == org_id))
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    return await _link_note_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        task_id=task_id,
        kind="subject",
    )


async def record_task_artifact(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    note_id: uuid.UUID,
) -> NoteTaskLink:
    """The task produced (or updated) this note. Proposal A's
    semantic, surfaced as an explicit operation. Both anchors must
    exist in this org."""
    await require_role(session, org_id, actor_id, Role.member)
    await _get_note(session, org_id=org_id, note_id=note_id)
    task = (
        await session.execute(select(Task).where(Task.id == task_id, Task.org_id == org_id))
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(MessageCode.TASK_NOT_FOUND)
    return await _link_note_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        note_id=note_id,
        task_id=task_id,
        kind="artifact",
    )


# ---------------------------------------------------------------------------
# Maturity worker tick (auto-transitions)
# ---------------------------------------------------------------------------


async def tick_maturity_transitions(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    now: dt.datetime | None = None,
    seed_to_growing_days: int = 7,
    growing_to_dormant_days: int = 60,
) -> dict[str, int]:
    """Apply the garden seasonal rules (docs/adr/0029 D2):

    - ``seed`` notes touched in the last ``seed_to_growing_days``
      (proxied by ``updated_at``) become ``growing``.
    - ``growing`` or ``mature`` notes whose ``updated_at`` is older
      than ``growing_to_dormant_days`` become ``dormant``.
    - ``dormant`` notes touched recently become ``growing``.

    Does NOT promote to ``mature`` (D2: that transition is always
    manual). Promoted/transplanted notes (``promoted_at IS NOT
    NULL``) are skipped.

    Returns counters per transition for telemetry/log.
    """
    now = now or dt.datetime.now(dt.UTC)
    seed_threshold = now - dt.timedelta(days=seed_to_growing_days)
    dormant_threshold = now - dt.timedelta(days=growing_to_dormant_days)
    out: dict[str, int] = {"seed_to_growing": 0, "to_dormant": 0, "dormant_to_growing": 0}

    # seed -> growing (touched recently)
    seed_to_growing = (
        (
            await session.execute(
                select(Note).where(
                    Note.org_id == org_id,
                    # An un-approved proposal does not age: its clock starts
                    # when a human accepts it (task a186c989).
                    effective_note_clause(),
                    Note.promoted_at.is_(None),
                    Note.maturity == NoteMaturity.seed.value,
                    Note.updated_at > seed_threshold,
                )
            )
        )
        .scalars()
        .all()
    )
    for n in seed_to_growing:
        n.maturity = NoteMaturity.growing.value
        out["seed_to_growing"] += 1

    # growing / mature -> dormant (untouched)
    to_dormant = (
        (
            await session.execute(
                select(Note).where(
                    Note.org_id == org_id,
                    # An un-approved proposal does not age: its clock starts
                    # when a human accepts it (task a186c989).
                    effective_note_clause(),
                    Note.promoted_at.is_(None),
                    Note.maturity.in_([NoteMaturity.growing.value, NoteMaturity.mature.value]),
                    Note.updated_at < dormant_threshold,
                    # Invariant (task 8a26c000): never auto-dormant a live
                    # note (one with an open linked task).
                    ~note_inert.open_work_exists(Note.id),
                )
            )
        )
        .scalars()
        .all()
    )
    for n in to_dormant:
        n.maturity = NoteMaturity.dormant.value
        out["to_dormant"] += 1

    # dormant -> growing (recovered)
    dormant_to_growing = (
        (
            await session.execute(
                select(Note).where(
                    Note.org_id == org_id,
                    # An un-approved proposal does not age: its clock starts
                    # when a human accepts it (task a186c989).
                    effective_note_clause(),
                    Note.promoted_at.is_(None),
                    Note.maturity == NoteMaturity.dormant.value,
                    Note.updated_at > seed_threshold,
                )
            )
        )
        .scalars()
        .all()
    )
    for n in dormant_to_growing:
        n.maturity = NoteMaturity.growing.value
        out["dormant_to_growing"] += 1

    if any(v > 0 for v in out.values()):
        await session.flush()
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="garden",
            entity_id=None,
            action="maturity_tick",
            diff={k: str(v) for k, v in out.items() if v > 0},
        )
    return out


__all__ = [
    "derive_task_from_note",
    "derived_task_ids_for_notes",
    "link_notes",
    "linked_task_counts_for_notes",
    "list_note_links",
    "list_note_task_links",
    "list_workspace_note_links",
    "promote_note_to_task",
    "record_task_artifact",
    "set_maturity",
    "start_task_on_note",
    "tick_maturity_transitions",
    "unlink_note_task",
    "unlink_notes",
]


# Quiet "unused" check for ``Any`` import (kept for future signature
# extension on the audit diff payloads).
_: Any = None
