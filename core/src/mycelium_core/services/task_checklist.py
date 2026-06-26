"""Checklist items attached to a task.

A task has two independent fields: ``description`` (markdown) and the
checklist (this module). The two surfaces are exposed as tabs in the
SPA task view; nothing else changes about the task model. Items are
lightweight (text + done + position), never sub-tasks.

Operations are atomic per-item: voice / agent automations call the
dedicated endpoints (or MCP tools) instead of patching the task's
description text. This keeps add / check / remove free from text-diff
races and removes the need for stable line-number IDs.

RBAC: the same ``member``-or-above gate that protects task mutations
applies. RLS scopes every read/write to the current org. Optimistic
concurrency uses ``version`` on the item row (mutations through
``update_item`` only); ``add`` / ``delete`` / ``reorder`` / ``clear_done``
do not need it (single-row insert/delete, or full-set rewrite).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.concurrency import optimistic_update
from mycelium_core.errors import DomainError, NotFoundError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.membership import Role
from mycelium_core.models.task_checklist_item import TaskChecklistItem
from mycelium_core.services import audit
from mycelium_core.services import notes as notes_svc
from mycelium_core.services import task_search as _task_search
from mycelium_core.services.rbac import require_role
from mycelium_core.services.tasks import get_task

# Position step between consecutive items so insertions in the middle
# can stay gap-based without a full rewrite. ``_next_position`` appends
# at +``_POSITION_STEP`` past the current max; ``reorder_items`` redoes
# the whole sequence with this step.
_POSITION_STEP = 1024


async def _validate_owner(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID | None,
    note_id: uuid.UUID | None,
) -> None:
    """Exactly one owner (task XOR note) must be given and must exist.
    ``get_task`` / ``get_note`` raise NotFoundError for a missing or
    soft-deleted owner, so reads/writes behave like the rest of the
    task / note surface."""
    if (task_id is None) == (note_id is None):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if task_id is not None:
        await get_task(session, org_id=org_id, task_id=task_id)
    else:
        assert note_id is not None  # noqa: S101 (XOR check above guarantees it)
        await notes_svc.get_note(session, org_id=org_id, note_id=note_id)


def _owner_clause(task_id: uuid.UUID | None, note_id: uuid.UUID | None):  # type: ignore[no-untyped-def]
    """SQLAlchemy filter for the polymorphic owner column."""
    if task_id is not None:
        return TaskChecklistItem.task_id == task_id
    return TaskChecklistItem.note_id == note_id


async def list_items(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
) -> list[TaskChecklistItem]:
    """All items of one owner (task XOR note), ordered by position then
    created_at."""
    await _validate_owner(session, org_id=org_id, task_id=task_id, note_id=note_id)
    stmt = (
        select(TaskChecklistItem)
        .where(_owner_clause(task_id, note_id))
        .order_by(TaskChecklistItem.position, TaskChecklistItem.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def items_by_task(
    session: AsyncSession,
    *,
    task_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[TaskChecklistItem]]:
    """Batch-load items for the given tasks. Used by the task serializer
    so a list endpoint doesn't issue one query per task."""
    if not task_ids:
        return {}
    stmt = (
        select(TaskChecklistItem)
        .where(TaskChecklistItem.task_id.in_(task_ids))
        .order_by(TaskChecklistItem.position, TaskChecklistItem.created_at)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    out: dict[uuid.UUID, list[TaskChecklistItem]] = {tid: [] for tid in task_ids}
    for r in rows:
        # The query filters task_id IN task_ids, so task_id is never None
        # here; the guard keeps mypy honest about the nullable column.
        if r.task_id is not None:
            out.setdefault(r.task_id, []).append(r)
    return out


async def _next_position(
    session: AsyncSession,
    *,
    task_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
) -> int:
    current_max = (
        await session.execute(
            select(TaskChecklistItem.position)
            .where(_owner_clause(task_id, note_id))
            .order_by(TaskChecklistItem.position.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (current_max or 0) + _POSITION_STEP


async def add_item(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
    text: str,
    body: str | None = None,
    position: int | None = None,
) -> TaskChecklistItem:
    await require_role(session, org_id, actor_id, Role.member)
    await _validate_owner(session, org_id=org_id, task_id=task_id, note_id=note_id)
    clean = text.strip()
    if not clean:
        raise DomainError(MessageCode.CHECKLIST_ITEM_TEXT_EMPTY)
    pos = (
        position
        if position is not None
        else await _next_position(session, task_id=task_id, note_id=note_id)
    )
    item = TaskChecklistItem(
        org_id=org_id,
        task_id=task_id,
        note_id=note_id,
        text=clean,
        body=(body.strip() or None) if body is not None else None,
        done=False,
        position=pos,
        created_by=actor_id,
    )
    session.add(item)
    await session.flush()
    # The ORM ``after_insert`` listener in task_search marks the parent
    # task dirty for task-owned items (and no-ops on a null task_id), so
    # no explicit mark is needed here.
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_checklist_item",
        entity_id=item.id,
        action="create",
        diff={
            "owner": ("task" if task_id is not None else "note"),
            "owner_id": str(task_id or note_id),
            "text": clean,
            "position": pos,
        },
    )
    return item


async def _get_item(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
) -> TaskChecklistItem:
    conds = [TaskChecklistItem.id == item_id]
    # RLS already scopes to the org; the owner filter (when given) is
    # defence-in-depth so an item can't be patched through the wrong
    # owner's route.
    if task_id is not None:
        conds.append(TaskChecklistItem.task_id == task_id)
    if note_id is not None:
        conds.append(TaskChecklistItem.note_id == note_id)
    item = (await session.execute(select(TaskChecklistItem).where(*conds))).scalar_one_or_none()
    if item is None:
        raise NotFoundError(MessageCode.CHECKLIST_ITEM_NOT_FOUND)
    return item


async def update_item(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    item_id: uuid.UUID,
    expected_version: int,
    task_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
    text: str | None = None,
    body: str | None = None,
    done: bool | None = None,
    position: int | None = None,
) -> TaskChecklistItem:
    """Patch a single item. ``done`` transitions stamp ``done_at`` /
    ``done_by`` (cleared on uncheck). ``body`` is the optional markdown
    comment (empty string clears it). Optimistic concurrency on
    ``version``: a stale write raises ConflictError -> HTTP 409."""
    await require_role(session, org_id, actor_id, Role.member)
    if task_id is not None or note_id is not None:
        await _validate_owner(session, org_id=org_id, task_id=task_id, note_id=note_id)
    item = await _get_item(session, item_id=item_id, task_id=task_id, note_id=note_id)
    values: dict[str, object] = {}
    diff: dict[str, object] = {}
    if text is not None:
        clean = text.strip()
        if not clean:
            raise DomainError(MessageCode.CHECKLIST_ITEM_TEXT_EMPTY)
        if clean != item.text:
            values["text"] = clean
            diff["text"] = clean
    if body is not None:
        # Empty string clears the comment; otherwise store trimmed.
        new_body = body.strip() or None
        if new_body != item.body:
            values["body"] = new_body
            diff["body"] = bool(new_body)
    if done is not None and done != item.done:
        values["done"] = done
        if done:
            values["done_at"] = dt.datetime.now(dt.UTC)
            values["done_by"] = actor_id
        else:
            values["done_at"] = None
            values["done_by"] = None
        diff["done"] = done
    if position is not None and position != item.position:
        values["position"] = position
        diff["position"] = position
    if not values:
        return item
    new_version = await optimistic_update(
        session,
        TaskChecklistItem,
        pk=item.id,
        expected_version=expected_version,
        values=values,
    )
    # Core UPDATE bypasses the mapper listener; mark the parent task so
    # the resync re-renders the blob with the new item text/done state.
    # Note-owned items don't feed the task FTS, so skip the mark.
    if item.task_id is not None:
        _task_search.mark_task_dirty(session, item.task_id)
    # Refresh so the caller sees the post-update state. ``refresh`` is
    # async-aware in SQLAlchemy 2.x and re-issues a SELECT for the
    # given attributes, bypassing the identity-map cache.
    await session.refresh(item)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_checklist_item",
        entity_id=item.id,
        action="update",
        diff={**diff, "version": new_version},
    )
    return item


async def delete_item(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    item_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    if task_id is not None or note_id is not None:
        await _validate_owner(session, org_id=org_id, task_id=task_id, note_id=note_id)
    item = await _get_item(session, item_id=item_id, task_id=task_id, note_id=note_id)
    # ORM delete fires the task_search after_delete listener (task-owned
    # items reindex; note-owned no-op), so no explicit mark here.
    await session.delete(item)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_checklist_item",
        entity_id=item_id,
        action="delete",
    )


async def clear_done(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
) -> int:
    """Drop every item already marked done. Returns the count for the
    caller's UX (e.g. toast "Removed N completed items")."""
    await require_role(session, org_id, actor_id, Role.member)
    await _validate_owner(session, org_id=org_id, task_id=task_id, note_id=note_id)
    result = await session.execute(
        delete(TaskChecklistItem)
        .where(
            _owner_clause(task_id, note_id),
            TaskChecklistItem.done.is_(True),
        )
        .returning(TaskChecklistItem.id)
    )
    removed_ids = [r[0] for r in result.all()]
    await session.flush()
    if removed_ids:
        # Core DELETE bypasses the mapper listener; reindex task-owned
        # only (note checklists don't feed the task FTS).
        if task_id is not None:
            _task_search.mark_task_dirty(session, task_id)
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="task_checklist",
            entity_id=(task_id or note_id),
            action="clear_done",
            diff={"removed": len(removed_ids)},
        )
    return len(removed_ids)


async def reorder_items(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    note_id: uuid.UUID | None = None,
    ordered_ids: Sequence[uuid.UUID],
) -> list[TaskChecklistItem]:
    """Rewrite ``position`` for every item of one owner (task XOR note)
    using the order of ``ordered_ids``. The payload must list exactly
    the owner's current items (no missing, no extras); otherwise raises
    ``CHECKLIST_REORDER_MISMATCH`` so a stale UI doesn't silently drop
    items added in another tab."""
    await require_role(session, org_id, actor_id, Role.member)
    await _validate_owner(session, org_id=org_id, task_id=task_id, note_id=note_id)
    current = list(
        (await session.execute(select(TaskChecklistItem).where(_owner_clause(task_id, note_id))))
        .scalars()
        .all()
    )
    current_ids = {it.id for it in current}
    payload_ids = list(ordered_ids)
    if set(payload_ids) != current_ids or len(payload_ids) != len(current_ids):
        raise DomainError(MessageCode.CHECKLIST_REORDER_MISMATCH)
    by_id = {it.id: it for it in current}
    for idx, iid in enumerate(payload_ids):
        item = by_id[iid]
        new_pos = (idx + 1) * _POSITION_STEP
        if item.position == new_pos:
            continue
        # Use optimistic_update so the version increments and any
        # concurrent client editing a single item sees a 409.
        await optimistic_update(
            session,
            TaskChecklistItem,
            pk=item.id,
            expected_version=item.version,
            values={"position": new_pos},
        )
    # Position-only reorder doesn't change rendered text (positions
    # drive ordering, not the bullet text), so the resync's
    # content_hash will short-circuit; the mark is still needed because
    # the listener path didn't fire. Task-owned only (note FTS separate).
    if task_id is not None:
        _task_search.mark_task_dirty(session, task_id)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_checklist",
        entity_id=(task_id or note_id),
        action="reorder",
        diff={"count": len(payload_ids)},
    )
    # Re-read so the caller sees the final positions / versions.
    return await list_items(session, org_id=org_id, task_id=task_id, note_id=note_id)
