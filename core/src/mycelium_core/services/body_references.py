"""The projection of references written in bodies (ADR-0073).

What a reference IS is decided by :mod:`reference_extract`. This module
keeps ``body_reference`` exactly in step with the bodies it is computed
from, resolves each occurrence once, and answers the reads built on it.

Freshness (decision 4)
    The projection is a second consumer of the dirty sets that already keep
    the search index in step with the text. ``note_search`` and
    ``task_search`` record every part and task their listeners and explicit
    marks see into this module's keys as well as their own
    (:func:`record_part`, :func:`record_task`), and
    :func:`flush_body_reference_dirty` drains them in ``db.tenant_session``
    before commit. A backlink therefore exists exactly when the body that
    holds it has committed, in the same transaction. A path that forgets to
    mark is caught by the drift count (:func:`count_drift`), whose expected
    value is zero, and repaired by :func:`repair_drift`.

Resolution (decision 3)
    An occurrence is resolved when it is first projected, and keeps its
    target for as long as its ``(syntax, token, ordinal)`` is in the body: a
    prefix that later collides with a new entity does not move it. A dangling
    occurrence is looked up again on the next projection (finding a target
    that now exists moves nothing a person meant). An ambiguous one stays
    ambiguous until a person chooses (:func:`choose_target`), and the choice
    is carried over like a resolution.

    * ``mention``: the kind and the full id are written; resolved when the
      row exists in any state (the read side decides what is shown),
      dangling otherwise.
    * ``chip`` / ``bare``: resolved against tasks and notes on the terms of
      the chip renderer and the normaliser (``lookup.resolve_prefix`` with
      the archive shelf), plus the bin, so that trashing and restoring the
      target does not change what was written. One match resolves, several
      are ambiguous. With none, a bare code is not a reference at all (the
      normaliser's gate), and a chip is recorded as dangling only when it
      has at least eight hex digits, the ADR-0038 convention: the renderer
      shows ``1234`` or ``cafe`` in backticks as plain code, so they are not
      references a person wrote.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import ColumnElement, SQLColumnExpression, and_, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, aliased

from mycelium_core.errors import ConflictError, DomainError, NotFoundError, UnprocessableError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.body_reference import BodyReference, BodyReferenceSource
from mycelium_core.models.note import Note
from mycelium_core.models.note_link import NoteNoteLink, NoteTaskLink
from mycelium_core.models.note_part import NotePart
from mycelium_core.models.tag import Tag
from mycelium_core.models.task import Task
from mycelium_core.services import lookup as lookup_svc
from mycelium_core.services.note_effective import effective_note_clause
from mycelium_core.services.reference_extract import Reference, extract_references

SourceKind = Literal["note_part", "task"]
TargetKind = Literal["note", "task", "tag"]

PART_DIRTY_KEY = "body_reference_part_dirty"
TASK_DIRTY_KEY = "body_reference_task_dirty"

#: A chip with fewer hex digits than this that resolves to nothing is plain
#: inline code, not a dangling reference (see the module docstring).
DANGLING_CHIP_MIN_HEX = lookup_svc.IDENTIFIER_MIN_HEX


# ---------------------------------------------------------------- marking


def record_part(session: Session | None, part_id: uuid.UUID | None) -> None:
    """Called from ``note_search``'s single recording point, so every path
    that schedules a part for re-indexing schedules it here too."""
    if session is None or part_id is None:
        return
    session.info.setdefault(PART_DIRTY_KEY, set()).add(part_id)


def record_task(session: Session | None, task_id: uuid.UUID | None) -> None:
    """Called from ``task_search``'s single recording point."""
    if session is None or task_id is None:
        return
    session.info.setdefault(TASK_DIRTY_KEY, set()).add(task_id)


def body_sha256(body: str | None) -> str:
    """The digest a source row records. Same bytes as the SQL in
    :func:`_drift_sources`: SHA-256 of the UTF-8 body, NULL read as empty."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


async def flush_body_reference_dirty(session: AsyncSession) -> None:
    """Project every part and task marked in this transaction. Called from
    ``db.tenant_session`` (and ``tenant_checkpoint``) right before commit,
    after the search flushes. Pops its sets, so it is re-entry safe; an
    unusable session (a contained savepoint failure) skips, and the drift
    verifier picks the sources up later."""
    from sqlalchemy.exc import InvalidRequestError

    info = session.sync_session.info
    part_ids: set[uuid.UUID] = info.pop(PART_DIRTY_KEY, set())
    task_ids: set[uuid.UUID] = info.pop(TASK_DIRTY_KEY, set())
    if not part_ids and not task_ids:
        return
    try:
        now = (await session.execute(select(func.now()))).scalar_one()
        for part_id in sorted(part_ids):
            await project_part(session, part_id=part_id, first_seen_at=now)
        for task_id in sorted(task_ids):
            await project_task(session, task_id=task_id, first_seen_at=now)
    except InvalidRequestError:
        return


# ---------------------------------------------------------------- projecting


@dataclass(frozen=True, slots=True)
class _Resolution:
    status: str
    target_kind: str | None
    target_id: uuid.UUID | None


async def project_part(
    session: AsyncSession, *, part_id: uuid.UUID, first_seen_at: datetime.datetime | None
) -> None:
    """Bring the projection of one note part in step with its body. A part
    that no longer exists has nothing to do: its source row went with it
    (``ON DELETE CASCADE``)."""
    row = (
        await session.execute(select(NotePart.org_id, NotePart.body).where(NotePart.id == part_id))
    ).first()
    if row is None:
        return
    org_id, body = row
    await _project(
        session,
        org_id=org_id,
        source_kind="note_part",
        source_value=part_id,
        body=body,
        first_seen_at=first_seen_at,
    )


async def project_task(
    session: AsyncSession, *, task_id: uuid.UUID, first_seen_at: datetime.datetime | None
) -> None:
    """Bring the projection of one task description in step with it."""
    row = (
        await session.execute(select(Task.org_id, Task.description).where(Task.id == task_id))
    ).first()
    if row is None:
        return
    org_id, body = row
    await _project(
        session,
        org_id=org_id,
        source_kind="task",
        source_value=task_id,
        body=body,
        first_seen_at=first_seen_at,
    )


async def _project(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    source_kind: SourceKind,
    source_value: uuid.UUID,
    body: str | None,
    first_seen_at: datetime.datetime | None,
) -> None:
    digest = body_sha256(body)
    source_col = (
        BodyReferenceSource.note_part_id
        if source_kind == "note_part"
        else BodyReferenceSource.task_id
    )
    # Fast path without a lock: the body was projected as it is.
    current = (
        await session.execute(
            select(BodyReferenceSource.body_sha256).where(source_col == source_value)
        )
    ).scalar_one_or_none()
    if current == digest:
        return
    # Two transactions can project one body at once: a person's edit and the
    # repair sweep, or two edits racing on a freshly created part. The
    # source row is created with ON CONFLICT DO NOTHING (the loser waits for
    # the winner's commit instead of failing on the unique index) and then
    # locked, so the occurrences of one source are only ever rewritten by one
    # transaction at a time. A unique violation here would roll back the
    # person's edit, which is the one outcome the projection must never cause.
    values: dict[str, object] = {"org_id": org_id, "body_sha256": "", "id": uuid.uuid4()}
    values[source_col.key] = source_value
    where = text(f"{source_col.key} IS NOT NULL")
    await session.execute(
        pg_insert(BodyReferenceSource)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[source_col.key], index_where=where)
    )
    source = (
        await session.execute(
            select(BodyReferenceSource)
            .where(source_col == source_value)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if source.body_sha256 == digest:
        return  # projected by the transaction we waited for
    refs = extract_references(body or "")
    source.body_sha256 = digest
    source.computed_at = func.now()
    rows = (
        await session.execute(select(BodyReference).where(BodyReference.source_id == source.id))
    ).scalars()
    existing: dict[tuple[str, str, int], BodyReference] = {
        (r.syntax, r.token, r.ordinal): r for r in rows
    }

    keep: set[tuple[str, str, int]] = set()
    to_resolve = [
        r
        for r in refs
        if (r.syntax, r.token, r.ordinal) not in existing
        or existing[(r.syntax, r.token, r.ordinal)].status == "dangling"
    ]
    resolved = await _resolve_all(session, to_resolve)
    for ref in refs:
        key = (ref.syntax, ref.token, ref.ordinal)
        prior = existing.get(key)
        anchor = ref.anchor
        if prior is not None and prior.status != "dangling":
            keep.add(key)
            prior.quote_exact = anchor.exact if anchor else None
            prior.quote_prefix = anchor.prefix if anchor else None
            prior.quote_suffix = anchor.suffix if anchor else None
            continue
        res = resolved.get((ref.syntax, ref.token, ref.mention_kind))
        if res is None:
            continue  # not a reference (an unresolving bare code or short chip)
        if prior is not None:
            keep.add(key)
            prior.status = res.status
            prior.target_kind = res.target_kind
            prior.target_id = res.target_id
            prior.quote_exact = anchor.exact if anchor else None
            prior.quote_prefix = anchor.prefix if anchor else None
            prior.quote_suffix = anchor.suffix if anchor else None
            continue
        keep.add(key)
        session.add(
            BodyReference(
                org_id=org_id,
                source_id=source.id,
                syntax=ref.syntax,
                token=ref.token,
                ordinal=ref.ordinal,
                target_kind=res.target_kind,
                target_id=res.target_id,
                status=res.status,
                quote_exact=anchor.exact if anchor else None,
                quote_prefix=anchor.prefix if anchor else None,
                quote_suffix=anchor.suffix if anchor else None,
                first_seen_at=first_seen_at,
            )
        )
    gone = [r.id for k, r in existing.items() if k not in keep]
    if gone:
        await session.execute(delete(BodyReference).where(BodyReference.id.in_(gone)))
    await session.flush()


async def _resolve_all(
    session: AsyncSession, refs: Iterable[Reference]
) -> dict[tuple[str, str, str | None], _Resolution]:
    """Resolve each distinct ``(syntax, token, kind)`` once. A key missing
    from the answer means the occurrence is not a reference."""
    out: dict[tuple[str, str, str | None], _Resolution] = {}
    for ref in refs:
        key = (ref.syntax, ref.token, ref.mention_kind)
        if key in out:
            continue
        res = await _resolve_one(session, ref)
        if res is not None:
            out[key] = res
    return out


async def _resolve_one(session: AsyncSession, ref: Reference) -> _Resolution | None:
    if ref.syntax == "mention":
        target = uuid.UUID(ref.token)
        id_col = {"note": Note.id, "task": Task.id, "tag": Tag.id}[ref.mention_kind or "note"]
        found = (await session.execute(select(id_col).where(id_col == target))).first()
        if found is None:
            return _Resolution("dangling", None, None)
        return _Resolution("resolved", ref.mention_kind, target)
    try:
        prefix = lookup_svc.normalise_prefix(ref.token)
    except DomainError:
        return None
    matches = await lookup_svc.resolve_prefix(
        session,
        prefix=prefix,
        kinds=("task", "note"),
        include_archived=True,
        include_deleted=True,
        limit=2,
    )
    if len(matches) == 1:
        return _Resolution("resolved", matches[0].kind, matches[0].id)
    if len(matches) > 1:
        return _Resolution("ambiguous", None, None)
    hex_digits = len(ref.token.replace("-", ""))
    if ref.syntax == "chip" and hex_digits >= DANGLING_CHIP_MIN_HEX:
        return _Resolution("dangling", None, None)
    return None


# ---------------------------------------------------------------- drift


def _sql_sha256(column: SQLColumnExpression[Any]) -> ColumnElement[str]:
    """The digest :func:`body_sha256` computes, in SQL."""
    return func.encode(func.sha256(func.convert_to(func.coalesce(column, ""), "UTF8")), "hex")


async def _drift_sources(
    session: AsyncSession, *, org_id: uuid.UUID, limit: int | None = None
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Parts and tasks whose projection is missing or was computed from a
    different body. RLS scopes both to the workspace; ``org_id`` is
    repeated so the plan uses the org indexes."""
    parts_q = (
        select(NotePart.id)
        .outerjoin(BodyReferenceSource, BodyReferenceSource.note_part_id == NotePart.id)
        .where(
            NotePart.org_id == org_id,
            or_(
                BodyReferenceSource.id.is_(None),
                BodyReferenceSource.body_sha256 != _sql_sha256(NotePart.body),
            ),
        )
        .order_by(NotePart.id)
        .limit(limit)
    )
    tasks_q = (
        select(Task.id)
        .outerjoin(BodyReferenceSource, BodyReferenceSource.task_id == Task.id)
        .where(
            Task.org_id == org_id,
            or_(
                BodyReferenceSource.id.is_(None),
                BodyReferenceSource.body_sha256 != _sql_sha256(Task.description),
            ),
        )
        .order_by(Task.id)
        .limit(limit)
    )
    parts = list((await session.execute(parts_q)).scalars())
    tasks = list((await session.execute(tasks_q)).scalars())
    return parts, tasks


async def count_drift(session: AsyncSession, *, org_id: uuid.UUID) -> int:
    """How many bodies the projection does not match right now. Expected
    value zero; anything else names a write path that does not mark its
    source dirty (ADR-0073 decision 4), or a workspace not yet backfilled."""
    parts, tasks = await _drift_sources(session, org_id=org_id)
    return len(parts) + len(tasks)


@dataclass(frozen=True, slots=True)
class RepairReport:
    parts: int
    tasks: int


async def repair_drift(
    session: AsyncSession, *, org_id: uuid.UUID, limit: int | None = None
) -> RepairReport:
    """Re-project every body the projection does not match. Occurrences
    found here have ``first_seen_at`` NULL: when they were written is not
    known. This is also the backfill (decision 9)."""
    parts, tasks = await _drift_sources(session, org_id=org_id, limit=limit)
    for pid in parts:
        await project_part(session, part_id=pid, first_seen_at=None)
    for tid in tasks:
        await project_task(session, task_id=tid, first_seen_at=None)
    return RepairReport(parts=len(parts), tasks=len(tasks))


# ---------------------------------------------------------------- choosing


async def choose_target(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    reference_id: uuid.UUID,
    target_kind: TargetKind,
    target_id: uuid.UUID,
) -> BodyReference:
    """A person resolves an occurrence that was ambiguous when written
    (decision 3). The target must be one the token can name: its id starts
    with the token, and it is a task or a note. The body is not touched."""
    ref = (
        await session.execute(
            select(BodyReference).where(
                BodyReference.id == reference_id, BodyReference.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if ref is None:
        raise NotFoundError(MessageCode.REFERENCE_NOT_FOUND)
    # A mention names its target in full, and a resolution nobody chose was
    # fixed when it was written: only an ambiguous occurrence, or one a
    # person already chose for, takes a choice.
    if ref.syntax == "mention" or (ref.status != "ambiguous" and ref.chosen_by is None):
        raise ConflictError(MessageCode.REFERENCE_NOT_AMBIGUOUS)
    if target_kind not in ("note", "task") or not str(target_id).startswith(ref.token):
        raise UnprocessableError(MessageCode.REFERENCE_TARGET_INVALID)
    model: type[Note] | type[Task] = Note if target_kind == "note" else Task
    if (await session.execute(select(model.id).where(model.id == target_id))).first() is None:
        raise UnprocessableError(MessageCode.REFERENCE_TARGET_INVALID)
    ref.status = "resolved"
    ref.target_kind = target_kind
    ref.target_id = target_id
    ref.chosen_by = actor_id
    ref.chosen_at = func.now()
    await session.flush()
    return ref


# ---------------------------------------------------------------- reading


@dataclass(frozen=True, slots=True)
class WrittenBacklink:
    """One occurrence of a reference to the target, from a source a reader
    may follow. ``status`` is ``resolved``, or ``ambiguous`` when the
    occurrence could name the target among others and nobody has chosen."""

    reference_id: uuid.UUID
    status: str
    syntax: str
    source_kind: SourceKind
    note_id: uuid.UUID | None
    part_id: uuid.UUID | None
    task_id: uuid.UUID | None
    title: str | None
    source_archived: bool
    quote_exact: str | None
    quote_prefix: str | None
    quote_suffix: str | None
    also_typed: bool
    first_seen_at: datetime.datetime | None


async def written_backlinks(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    target_kind: TargetKind,
    target_id: uuid.UUID,
) -> list[WrittenBacklink]:
    """Every occurrence of a written reference to the target, from sources
    a reader may follow (decision 5). Note sources must be effective (not in
    the bin, not awaiting review), task sources not in the bin; archived
    sources are returned and labelled. Ordered by source, then position."""
    src_note = aliased(Note)
    token_matches = text("starts_with(:tid, body_reference.token)").bindparams(tid=str(target_id))
    targeting = or_(
        and_(
            BodyReference.status == "resolved",
            BodyReference.target_kind == target_kind,
            BodyReference.target_id == target_id,
        ),
        and_(
            BodyReference.status == "ambiguous",
            BodyReference.syntax != "mention",
            token_matches,
        )
        if target_kind in ("note", "task")
        else text("false"),
    )
    note_rows = (
        await session.execute(
            select(BodyReference, NotePart.id, src_note.id, src_note.title, src_note.is_archived)
            .join(BodyReferenceSource, BodyReferenceSource.id == BodyReference.source_id)
            .join(NotePart, NotePart.id == BodyReferenceSource.note_part_id)
            .join(src_note, src_note.id == NotePart.note_id)
            .where(
                BodyReference.org_id == org_id,
                targeting,
                effective_note_clause(entity=src_note),
            )
            .order_by(src_note.updated_at.desc(), NotePart.ord, BodyReference.ordinal)
        )
    ).all()
    task_rows = (
        await session.execute(
            select(BodyReference, Task.id, Task.title, Task.is_archived)
            .join(BodyReferenceSource, BodyReferenceSource.id == BodyReference.source_id)
            .join(Task, Task.id == BodyReferenceSource.task_id)
            .where(BodyReference.org_id == org_id, targeting, Task.deleted_at.is_(None))
            .order_by(Task.updated_at.desc(), BodyReference.ordinal)
        )
    ).all()
    typed_notes, typed_tasks = await _typed_partners(
        session,
        org_id=org_id,
        target_kind=target_kind,
        target_id=target_id,
        note_ids=[r[2] for r in note_rows],
        task_ids=[r[1] for r in task_rows],
    )
    out: list[WrittenBacklink] = []
    for ref, part_id, note_id, title, archived in note_rows:
        if target_kind == "note" and note_id == target_id:
            continue  # a note citing itself is not a backlink
        out.append(
            WrittenBacklink(
                reference_id=ref.id,
                status=ref.status,
                syntax=ref.syntax,
                source_kind="note_part",
                note_id=note_id,
                part_id=part_id,
                task_id=None,
                title=title,
                source_archived=bool(archived),
                quote_exact=ref.quote_exact,
                quote_prefix=ref.quote_prefix,
                quote_suffix=ref.quote_suffix,
                also_typed=note_id in typed_notes,
                first_seen_at=ref.first_seen_at,
            )
        )
    for ref, task_id, title, archived in task_rows:
        if target_kind == "task" and task_id == target_id:
            continue
        out.append(
            WrittenBacklink(
                reference_id=ref.id,
                status=ref.status,
                syntax=ref.syntax,
                source_kind="task",
                note_id=None,
                part_id=None,
                task_id=task_id,
                title=title,
                source_archived=bool(archived),
                quote_exact=ref.quote_exact,
                quote_prefix=ref.quote_prefix,
                quote_suffix=ref.quote_suffix,
                also_typed=task_id in typed_tasks,
                first_seen_at=ref.first_seen_at,
            )
        )
    return out


async def _typed_partners(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    target_kind: TargetKind,
    target_id: uuid.UUID,
    note_ids: Sequence[uuid.UUID],
    task_ids: Sequence[uuid.UUID],
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Which citing notes and tasks also hold a typed link to the target, in
    either direction, so a reader sees what the typed graph already knows."""
    typed_notes: set[uuid.UUID] = set()
    typed_tasks: set[uuid.UUID] = set()
    if target_kind == "note" and note_ids:
        rows = await session.execute(
            select(NoteNoteLink.parent_note_id, NoteNoteLink.child_note_id).where(
                NoteNoteLink.org_id == org_id,
                or_(
                    and_(
                        NoteNoteLink.parent_note_id == target_id,
                        NoteNoteLink.child_note_id.in_(note_ids),
                    ),
                    and_(
                        NoteNoteLink.child_note_id == target_id,
                        NoteNoteLink.parent_note_id.in_(note_ids),
                    ),
                ),
            )
        )
        for parent, child in rows:
            typed_notes.add(child if parent == target_id else parent)
    if target_kind == "note" and task_ids:
        typed_tasks = set(
            (
                await session.execute(
                    select(NoteTaskLink.task_id).where(
                        NoteTaskLink.org_id == org_id,
                        NoteTaskLink.note_id == target_id,
                        NoteTaskLink.task_id.in_(task_ids),
                    )
                )
            ).scalars()
        )
    if target_kind == "task" and note_ids:
        typed_notes = set(
            (
                await session.execute(
                    select(NoteTaskLink.note_id).where(
                        NoteTaskLink.org_id == org_id,
                        NoteTaskLink.task_id == target_id,
                        NoteTaskLink.note_id.in_(note_ids),
                    )
                )
            ).scalars()
        )
    return typed_notes, typed_tasks
