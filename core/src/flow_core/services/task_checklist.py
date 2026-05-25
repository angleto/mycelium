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

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.task_checklist_item import TaskChecklistItem
from flow_core.services import audit
from flow_core.services import task_search as _task_search
from flow_core.services.rbac import require_role
from flow_core.services.tasks import get_task

# Position step between consecutive items so insertions in the middle
# can stay gap-based without a full rewrite. ``_next_position`` appends
# at +``_POSITION_STEP`` past the current max; ``reorder_items`` redoes
# the whole sequence with this step.
_POSITION_STEP = 1024


async def list_items(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
) -> list[TaskChecklistItem]:
    """All items of a task, ordered by position then created_at."""
    # ``get_task`` raises NotFoundError if the task is missing or soft-
    # deleted: keep that contract here so reads behave like the rest of
    # the task surface.
    await get_task(session, org_id=org_id, task_id=task_id)
    stmt = (
        select(TaskChecklistItem)
        .where(TaskChecklistItem.task_id == task_id)
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
        out.setdefault(r.task_id, []).append(r)
    return out


async def _next_position(session: AsyncSession, *, task_id: uuid.UUID) -> int:
    current_max = (
        await session.execute(
            select(TaskChecklistItem.position)
            .where(TaskChecklistItem.task_id == task_id)
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
    task_id: uuid.UUID,
    text: str,
    position: int | None = None,
) -> TaskChecklistItem:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    clean = text.strip()
    if not clean:
        raise DomainError(MessageCode.CHECKLIST_ITEM_TEXT_EMPTY)
    pos = position if position is not None else await _next_position(session, task_id=task_id)
    item = TaskChecklistItem(
        org_id=org_id,
        task_id=task_id,
        text=clean,
        done=False,
        position=pos,
        created_by=actor_id,
    )
    session.add(item)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_checklist_item",
        entity_id=item.id,
        action="create",
        diff={"task_id": str(task_id), "text": clean, "position": pos},
    )
    return item


async def _get_item(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    item_id: uuid.UUID,
) -> TaskChecklistItem:
    item = (
        await session.execute(
            select(TaskChecklistItem).where(
                TaskChecklistItem.id == item_id,
                TaskChecklistItem.task_id == task_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise NotFoundError(MessageCode.CHECKLIST_ITEM_NOT_FOUND)
    return item


async def update_item(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    expected_version: int,
    text: str | None = None,
    done: bool | None = None,
    position: int | None = None,
) -> TaskChecklistItem:
    """Patch a single item. ``done`` transitions stamp ``done_at`` /
    ``done_by`` (cleared on uncheck). Optimistic concurrency on
    ``version``: a stale write raises ConflictError -> HTTP 409."""
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    item = await _get_item(session, task_id=task_id, item_id=item_id)
    values: dict[str, object] = {}
    diff: dict[str, object] = {}
    if text is not None:
        clean = text.strip()
        if not clean:
            raise DomainError(MessageCode.CHECKLIST_ITEM_TEXT_EMPTY)
        if clean != item.text:
            values["text"] = clean
            diff["text"] = clean
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
    _task_search.mark_task_dirty(session, task_id)
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
    task_id: uuid.UUID,
    item_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    item = await _get_item(session, task_id=task_id, item_id=item_id)
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
    task_id: uuid.UUID,
) -> int:
    """Drop every item already marked done. Returns the count for the
    caller's UX (e.g. toast "Removed N completed items")."""
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    result = await session.execute(
        delete(TaskChecklistItem)
        .where(
            TaskChecklistItem.task_id == task_id,
            TaskChecklistItem.done.is_(True),
        )
        .returning(TaskChecklistItem.id)
    )
    removed_ids = [r[0] for r in result.all()]
    await session.flush()
    if removed_ids:
        _task_search.mark_task_dirty(session, task_id)
        await audit.log(
            session,
            org_id=org_id,
            actor_id=actor_id,
            entity="task_checklist",
            entity_id=task_id,
            action="clear_done",
            diff={"removed": len(removed_ids)},
        )
    return len(removed_ids)


async def reorder_items(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    ordered_ids: Sequence[uuid.UUID],
) -> list[TaskChecklistItem]:
    """Rewrite ``position`` for every item of the task using the order
    of ``ordered_ids``. The payload must list exactly the task's
    current items (no missing, no extras); otherwise raises
    ``CHECKLIST_REORDER_MISMATCH`` so a stale UI doesn't silently drop
    items added in another tab."""
    await require_role(session, org_id, actor_id, Role.member)
    await get_task(session, org_id=org_id, task_id=task_id)
    current = list(
        (
            await session.execute(
                select(TaskChecklistItem).where(TaskChecklistItem.task_id == task_id)
            )
        )
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
    # the listener path didn't fire.
    _task_search.mark_task_dirty(session, task_id)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_checklist",
        entity_id=task_id,
        action="reorder",
        diff={"count": len(payload_ids)},
    )
    # Re-read so the caller sees the final positions / versions.
    return await list_items(session, org_id=org_id, task_id=task_id)
