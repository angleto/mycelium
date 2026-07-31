"""Structural tag assignment for tasks and notes (docs/adr/0003 unified
tags, docs/adr/0021 note perimeter).

The invariant this module owns: a TASK carries exactly one tag of kind
client and exactly one of kind project; a NOTE carries exactly one
client and AT MOST one project (a projectless note is a first-class
personal retrieval perimeter, ``memory_blobs.project_id`` NULL -- see
core/tests/test_f6b_notes.py). Whenever an entity carries a project,
its client IS that project's ``project_profile.client_tag_id``: the
project is the truth, the client is derived from it.

This module is the ONLY place allowed to INSERT or DELETE a
``task_tags`` / ``note_tags`` row whose tag is of kind client or
project. Routing every door (HTTP, MCP, CLI, importers) through here is
what makes the invariant hold; ``generic`` and ``memory_channel`` tags
stay unconstrained many-to-many and go through ``attach_generic`` /
``detach_generic``.

Not in scope: RBAC and entity existence. The calling service keeps its
``require_role`` + ``get_task`` / ``get_note`` gate before delegating
here, exactly as it does today. Audit is in scope: every write below
logs the same ``attach_tag`` / ``detach_tag`` action the previous
per-service code logged, so a caller must not log it a second time.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, insert, literal, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased

from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.note_tag import NoteTag
from mycelium_core.models.project_profile import ProjectProfile
from mycelium_core.models.tag import Tag, TagKind
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.services import audit, taxonomy

Entity = Literal["task", "note"]

_STRUCTURAL_KINDS: tuple[TagKind, ...] = (TagKind.client, TagKind.project)
# Free-form facets: no cardinality rule, no cross-check with the client
# or the project. A memory channel is just another facet here (its own
# vocabulary rules live in taxonomy).
_FREEFORM_KINDS: tuple[TagKind, ...] = (TagKind.generic, TagKind.memory_channel)


@dataclass(frozen=True, slots=True)
class Structural:
    """The structural pair an entity must end up with, plus the
    free-form tags the caller asked for.

    ``generic_ids`` is echoed back deduped and in request order purely
    so a create path can attach them after the pair; ``set_structural``
    does not touch them (they are unconstrained).
    """

    client_tag_id: uuid.UUID
    project_tag_id: uuid.UUID | None
    generic_ids: tuple[uuid.UUID, ...]


def _junction(
    entity: Entity,
) -> tuple[type[TaskTag] | type[NoteTag], InstrumentedAttribute[uuid.UUID]]:
    """Junction model + owner column. ``task_tags`` and ``note_tags``
    are the same shape (docs/adr/0003), so one mapping keeps the rest of
    the module entity-agnostic."""
    if entity == "task":
        return TaskTag, TaskTag.task_id
    return NoteTag, NoteTag.note_id


def _link(
    entity: Entity, *, org_id: uuid.UUID, entity_id: uuid.UUID, tag_id: uuid.UUID
) -> TaskTag | NoteTag:
    if entity == "task":
        return TaskTag(org_id=org_id, task_id=entity_id, tag_id=tag_id)
    return NoteTag(org_id=org_id, note_id=entity_id, tag_id=tag_id)


def _dedupe(ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Order-preserving dedupe: a caller repeating a tag id means the
    same tag, it is not an error."""
    return list(dict.fromkeys(ids))


async def _load_kinds(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, TagKind]:
    """``id -> kind`` for every requested tag in ONE query. RLS hides
    another workspace's tags, so a foreign id simply does not come back
    and is reported as TAG_NOT_FOUND -- the same answer the previous
    ``tasks._require_tag`` / ``notes.attach_tag`` lookups gave. Archived
    tags are still attachable: archiving only removes a tag from the
    pickers (``taxonomy.list_tags``), it never orphans what it holds."""
    if not ids:
        return {}
    rows = (await session.execute(select(Tag.id, Tag.kind).where(Tag.id.in_(list(ids))))).all()
    kinds: dict[uuid.UUID, TagKind] = {tag_id: kind for tag_id, kind in rows}
    if any(tag_id not in kinds for tag_id in ids):
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    return kinds


async def _kind_of(session: AsyncSession, tag_id: uuid.UUID) -> TagKind:
    kind = (await session.execute(select(Tag.kind).where(Tag.id == tag_id))).scalar_one_or_none()
    if kind is None:
        raise NotFoundError(MessageCode.TAG_NOT_FOUND)
    return kind


async def _client_of_project(session: AsyncSession, project_tag_id: uuid.UUID) -> uuid.UUID:
    client_tag_id = (
        await session.execute(
            select(ProjectProfile.client_tag_id).where(ProjectProfile.tag_id == project_tag_id)
        )
    ).scalar_one_or_none()
    if client_tag_id is None:
        # Every project has exactly one client. A project tag with no
        # profile row (or a NULL client) is broken taxonomy, not a
        # caller mistake we can paper over by inventing a client.
        raise DomainError(MessageCode.PROJECT_CLIENT_REQUIRED)
    return client_tag_id


async def _current_structural(
    session: AsyncSession, *, entity: Entity, entity_id: uuid.UUID
) -> dict[uuid.UUID, TagKind]:
    """The entity's currently attached client/project tags (id -> kind).
    Zero to two rows once the invariant holds; legacy rows in excess are
    simply replaced by the next ``set_structural``."""
    model, owner = _junction(entity)
    rows = (
        await session.execute(
            select(Tag.id, Tag.kind)
            .join(model, model.tag_id == Tag.id)
            .where(owner == entity_id, Tag.kind.in_(_STRUCTURAL_KINDS))
        )
    ).all()
    return {tag_id: kind for tag_id, kind in rows}


def _pick(current: dict[uuid.UUID, TagKind], kind: TagKind) -> uuid.UUID | None:
    for tag_id, tag_kind in current.items():
        if tag_kind is kind:
            return tag_id
    return None


async def resolve_structural(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity: Entity,
    requested: Sequence[uuid.UUID] = (),
    project_tag_id: uuid.UUID | None = None,
    client_tag_id: uuid.UUID | None = None,
) -> Structural:
    """Resolve an arbitrary bag of tag ids into the structural pair the
    entity must end up with, without writing anything.

    The project decides the client. A client the CALLER named (either in
    ``requested`` or as ``client_tag_id``) that disagrees with the
    resolved project's client is a contradiction stated in one call, so
    it is refused rather than silently dropped; moving an entity to
    another client is done by attaching that client's project
    (``move_to_project``), which is a MOVE, not an error.

    A task with no project gets the default one (no orphan tasks); a
    note keeps ``project_tag_id=None`` and falls back to the default
    client, which is the personal perimeter, not a defect.
    """
    ordered = _dedupe([*requested, *(t for t in (project_tag_id, client_tag_id) if t is not None)])
    kinds = await _load_kinds(session, ordered)
    if project_tag_id is not None and kinds[project_tag_id] is not TagKind.project:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    if client_tag_id is not None and kinds[client_tag_id] is not TagKind.client:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    projects = [t for t in ordered if kinds[t] is TagKind.project]
    clients = [t for t in ordered if kinds[t] is TagKind.client]
    if len(clients) > 1:
        raise DomainError(MessageCode.TAG_MULTIPLE_CLIENTS)
    if len(projects) > 1:
        raise DomainError(MessageCode.TAG_MULTIPLE_PROJECTS)
    project = projects[0] if projects else None
    named_client = clients[0] if clients else None
    if project is None and entity == "task":
        # No orphan tasks: a task with no project falls back to the
        # default "General" project under the default "Personal" client.
        project = await taxonomy.ensure_default_project(session, org_id=org_id, actor_id=actor_id)
    if project is not None:
        client = await _client_of_project(session, project)
        if named_client is not None and named_client != client:
            raise DomainError(MessageCode.TAG_CLIENT_PROJECT_MISMATCH)
    elif named_client is not None:
        client = named_client
    else:
        client = await taxonomy.ensure_default_client(session, org_id=org_id, actor_id=actor_id)
    return Structural(
        client_tag_id=client,
        project_tag_id=project,
        generic_ids=tuple(t for t in ordered if kinds[t] in _FREEFORM_KINDS),
    )


async def set_structural(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity: Entity,
    entity_id: uuid.UUID,
    structural: Structural,
    on_create: bool = False,
) -> None:
    """Write the resolved pair, replacing whatever client/project rows
    the entity had. Free-form tags are left alone.

    ``on_create`` suppresses the audit row: the entity's own ``create``
    row already records the genesis, and ``attach_tag`` is in the
    co-activity touch allow-list (services/coactivity.py). Logging one
    per creation would turn "was born in a project" into "someone worked
    on it", forging exactly the spurious clique between everything made
    in one session that the aggregator excludes ``create`` to avoid.
    """
    current = await _current_structural(session, entity=entity, entity_id=entity_id)
    desired = [structural.client_tag_id]
    if structural.project_tag_id is not None:
        desired.append(structural.project_tag_id)
    if set(current) == set(desired):
        # Idempotent re-assignment: nothing to write, nothing to log,
        # and the blobs already sit on the right perimeter -- the same
        # early exit the previous duplicate-row attach path took.
        return
    model, owner = _junction(entity)
    if current:
        await session.execute(
            delete(model).where(owner == entity_id, model.tag_id.in_(list(current)))
        )
        # The DELETE must reach the DB BEFORE the INSERTs: SQLAlchemy's
        # natural unit-of-work order is INSERT-then-DELETE, which would
        # expose an entity carrying two clients halfway through a swap.
        await session.flush()
    for tag_id in desired:
        session.add(_link(entity, org_id=org_id, entity_id=entity_id, tag_id=tag_id))
    await session.flush()
    if entity == "note" and _pick(current, TagKind.project) != structural.project_tag_id:
        # A project change moves the note's retrieval perimeter: re-scope
        # its indexed blobs now so peers see it immediately (task
        # 1d152747), and only once the final state is written -- reading
        # it between the DELETE and the INSERT would scope to nothing.
        # Lazy import: note_search imports notes, notes imports us.
        from mycelium_core.services import note_search

        await note_search.rescope_note_blobs(session, org_id=org_id, note_id=entity_id)
    if on_create:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity=entity,
        entity_id=entity_id,
        action="attach_tag" if set(desired) - set(current) else "detach_tag",
        diff={
            "client_tag_id": str(structural.client_tag_id),
            "project_tag_id": (
                str(structural.project_tag_id) if structural.project_tag_id is not None else None
            ),
        },
    )


async def attach_generic(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity: Entity,
    entity_id: uuid.UUID,
    tag_id: uuid.UUID,
    on_create: bool = False,
) -> None:
    """Attach a free-form facet. A client/project tag is refused here:
    it would bypass the structural resolution and can only be assigned
    through ``set_structural`` / ``move_to_project`` / ``set_client``.

    ``on_create`` suppresses the audit row for the same reason it does in
    ``set_structural``: tagging at birth is genesis, and the pre-invariant
    create paths wrote these junction rows with no audit row at all."""
    if await _kind_of(session, tag_id) not in _FREEFORM_KINDS:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    try:
        # Savepoint: re-attaching the same pair is idempotent, the
        # junction PK is the arbiter, and the outer transaction survives.
        async with session.begin_nested():
            session.add(_link(entity, org_id=org_id, entity_id=entity_id, tag_id=tag_id))
            await session.flush()
    except IntegrityError:
        return
    if on_create:
        return
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity=entity,
        entity_id=entity_id,
        action="attach_tag",
    )


async def detach_generic(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity: Entity,
    entity_id: uuid.UUID,
    tag_id: uuid.UUID,
) -> None:
    """Remove a free-form facet. Detaching a client or a project is not
    done here: for a task it is refused (TAG_STRUCTURAL_REQUIRED, the
    caller's gate), for a note's project it is ``clear_project``."""
    if await _kind_of(session, tag_id) not in _FREEFORM_KINDS:
        raise DomainError(MessageCode.TAG_KIND_MISMATCH)
    model, owner = _junction(entity)
    await session.execute(delete(model).where(owner == entity_id, model.tag_id == tag_id))
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity=entity,
        entity_id=entity_id,
        action="detach_tag",
    )


async def move_to_project(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity: Entity,
    entity_id: uuid.UUID,
    project_tag_id: uuid.UUID,
) -> None:
    """Attach a project = MOVE. A project of another client re-points
    the entity at that client too, atomically; it is deliberately not an
    error, because "this work is now for that project" is the user's
    intent and the client merely follows the project."""
    structural = await resolve_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity=entity,
        project_tag_id=project_tag_id,
    )
    await set_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity=entity,
        entity_id=entity_id,
        structural=structural,
    )


async def set_client(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entity: Entity,
    entity_id: uuid.UUID,
    client_tag_id: uuid.UUID,
) -> None:
    """Re-point the entity at a client, keeping its project. A client
    that contradicts the attached project is refused with
    TAG_CLIENT_PROJECT_MISMATCH: the project is the truth, so that move
    is expressed by attaching a project of the wanted client instead."""
    current = await _current_structural(session, entity=entity, entity_id=entity_id)
    structural = await resolve_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity=entity,
        project_tag_id=_pick(current, TagKind.project),
        client_tag_id=client_tag_id,
    )
    await set_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity=entity,
        entity_id=entity_id,
        structural=structural,
    )


async def rebind_project_client(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_tag_id: uuid.UUID,
    client_tag_id: uuid.UUID,
) -> None:
    """Re-tag every task and note carrying ``project_tag_id`` onto
    ``client_tag_id``, set-based.

    The caller is ``taxonomy.reassign_project_client``, which has just
    re-pointed ``project_profile.client_tag_id``: invariant (c) is a
    property of the subgraph, not of the profile row, so the carriers
    must follow in the SAME transaction or the commit-time guard
    rejects the move. This lives here, not in taxonomy, because it
    writes structural junction rows and nothing outside this module may
    (see the module docstring); it is set-based rather than a loop over
    ``set_client`` because a project can hold thousands of tasks, and
    every carrier is by construction resolving to the same client, so
    there is nothing per-row to adjudicate.

    Deleting EVERY client row of a carrier (not only the previous
    client's) also repairs a carrier that had already drifted onto a
    third client. Blobs are deliberately not re-scoped: a note's
    retrieval perimeter is its project (``memory_blobs.project_id``,
    docs/adr/0021), which this operation leaves untouched. No
    ``actor_id``, and no audit row: the semantic operation is the
    caller's ``reassign_project_client``, which logs it once with the
    previous and the new client; one row per dependent task would
    drown the log for a bulk taxonomy edit.
    """
    org_clients = select(Tag.id).where(Tag.org_id == org_id, Tag.kind == TagKind.client)
    task_carrier = aliased(TaskTag, name="carrier")
    await session.execute(
        delete(TaskTag).where(
            TaskTag.task_id.in_(
                select(task_carrier.task_id).where(task_carrier.tag_id == project_tag_id)
            ),
            TaskTag.tag_id.in_(org_clients),
        )
    )
    note_carrier = aliased(NoteTag, name="carrier")
    await session.execute(
        delete(NoteTag).where(
            NoteTag.note_id.in_(
                select(note_carrier.note_id).where(note_carrier.tag_id == project_tag_id)
            ),
            NoteTag.tag_id.in_(org_clients),
        )
    )
    # Same ordering rule as ``set_structural``: the DELETE must reach
    # the DB before the INSERT, or a carrier momentarily holds two
    # clients.
    await session.flush()
    new_client = literal(client_tag_id, TaskTag.tag_id.type)
    await session.execute(
        insert(TaskTag).from_select(
            ["org_id", "task_id", "tag_id"],
            select(TaskTag.org_id, TaskTag.task_id, new_client).where(
                TaskTag.tag_id == project_tag_id
            ),
        )
    )
    await session.execute(
        insert(NoteTag).from_select(
            ["org_id", "note_id", "tag_id"],
            select(NoteTag.org_id, NoteTag.note_id, new_client).where(
                NoteTag.tag_id == project_tag_id
            ),
        )
    )
    await session.flush()


async def clear_project(
    session: AsyncSession, *, org_id: uuid.UUID, actor_id: uuid.UUID, note_id: uuid.UUID
) -> None:
    """Un-share a note: drop its project, keep its client, send its
    blobs back to the personal (NULL project) perimeter. Notes only:
    a task without a project would be an orphan, which is why there is
    no task counterpart."""
    current = await _current_structural(session, entity="note", entity_id=note_id)
    if _pick(current, TagKind.project) is None:
        return
    client_tag_id = _pick(current, TagKind.client)
    if client_tag_id is None:
        # Pre-invariant data: a note whose client row is missing keeps a
        # client anyway (the workspace default) rather than becoming
        # clientless while we are here to fix it.
        client_tag_id = await taxonomy.ensure_default_client(
            session, org_id=org_id, actor_id=actor_id
        )
    await set_structural(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="note",
        entity_id=note_id,
        structural=Structural(client_tag_id=client_tag_id, project_tag_id=None, generic_ids=()),
    )
