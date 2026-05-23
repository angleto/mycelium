"""Note garden ecosystem operations (docs/adr/0029, P1).

Six named operations + maturity setter:

- ``set_maturity(note_id, maturity)``: manual override of the garden
  lifecycle. Auto-transitions (touched-recently / untouched-long /
  on-touch) live in a worker tick (see ``worker/garden.py``); this
  is the always-allowed manual setter.

- ``link_notes(parent, child, kind)`` / ``unlink_notes(...)``: typed
  M:N between notes (atom_of, references, replies_to, supersedes).
  Used to build the Zettelkasten structure (index note + atomic
  children, citation backlinks, threaded replies, supersession).

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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.identity import Identity
from flow_core.models.membership import Role
from flow_core.models.note import Note, NoteMaturity
from flow_core.models.note_link import (
    NOTE_NOTE_LINK_KINDS,
    NOTE_TASK_LINK_KINDS,
    NoteNoteLink,
    NoteTaskLink,
)
from flow_core.models.task import Task
from flow_core.services import audit
from flow_core.services import tasks as tasks_svc
from flow_core.services.rbac import require_role

_VALID_MATURITY: frozenset[str] = frozenset(m.value for m in NoteMaturity)


async def _get_note(session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID) -> Note:
    row = (
        await session.execute(
            select(Note).where(
                Note.id == note_id,
                Note.org_id == org_id,
                Note.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(MessageCode.MEMORY_NOT_FOUND)
    return row


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
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    note = await _get_note(session, org_id=org_id, note_id=note_id)
    if note.promoted_at is not None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
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
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if parent_note_id == child_note_id:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    # Both notes must exist in this org (FK alone does not enforce
    # org match; defence in depth).
    await _get_note(session, org_id=org_id, note_id=parent_note_id)
    await _get_note(session, org_id=org_id, note_id=child_note_id)

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
        raise DomainError(MessageCode.DOMAIN_ERROR)
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
        raise DomainError(MessageCode.DOMAIN_ERROR)
    stmt = select(NoteTaskLink).where(NoteTaskLink.org_id == org_id)
    if note_id is not None:
        stmt = stmt.where(NoteTaskLink.note_id == note_id)
    if task_id is not None:
        stmt = stmt.where(NoteTaskLink.task_id == task_id)
    return list((await session.execute(stmt)).scalars().all())


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
    alive. Inherits the note's client/project from its first project
    tag mapping if any (otherwise the user's default Personal /
    General via ``tasks_svc.create_task``)."""
    await require_role(session, org_id, actor_id, Role.member)
    note = await _get_note(session, org_id=org_id, note_id=note_id)
    task = await tasks_svc.create_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        title=title,
        description=description,
        estimate_effort_h=estimate_effort_h,
        tag_ids=extra_tag_ids or [],
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
    the note is marked read-only (``promoted_at = now``).

    The title defaults to the note's title (or a derived snippet of
    the transcript when the title is empty).
    """
    await require_role(session, org_id, actor_id, Role.member)
    note = await _get_note(session, org_id=org_id, note_id=note_id)
    if note.promoted_at is not None:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    chosen_title = (title or note.title or "").strip()
    if not chosen_title:
        # Derive from the first non-empty line of the transcript.
        body = (note.transcript or "").strip().splitlines()
        chosen_title = body[0][:120] if body else "promoted note"
    task = await tasks_svc.create_task(
        session,
        org_id=org_id,
        actor_id=actor_id,
        title=chosen_title,
        description=note.summary or note.transcript,
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
                    Note.deleted_at.is_(None),
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
                    Note.deleted_at.is_(None),
                    Note.promoted_at.is_(None),
                    Note.maturity.in_([NoteMaturity.growing.value, NoteMaturity.mature.value]),
                    Note.updated_at < dormant_threshold,
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
                    Note.deleted_at.is_(None),
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
    "link_notes",
    "list_note_links",
    "list_note_task_links",
    "promote_note_to_task",
    "record_task_artifact",
    "set_maturity",
    "start_task_on_note",
    "tick_maturity_transitions",
    "unlink_notes",
]


# Quiet "unused" check for ``Any`` import (kept for future signature
# extension on the audit diff payloads).
_: Any = None
