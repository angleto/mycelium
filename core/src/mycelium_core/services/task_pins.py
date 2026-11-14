"""A person's pinned tasks (migration 0027).

Pinning is the person keeping a task at hand: it writes one row of their own
and nothing on the task. Both directions are idempotent, because the control
that sets and clears a pin is a toggle pressed from several lists at once,
and a second press racing the first must land on the state the person sees
rather than on an error.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.task_pin import TaskPin
from mycelium_core.services import tasks as tasks_svc


async def pin(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, task_id: uuid.UUID
) -> dt.datetime:
    """Pin a task for ``user_id``. Pinning a pinned task keeps the first
    pin's time, so the order of the pinned list is the order the person
    pinned things in, not the order they last pressed the button. A task in
    the bin cannot be pinned (``TASK_NOT_FOUND``); a closed or archived one
    can, which is the point: the person reaches it from here."""
    await tasks_svc.get_task(session, org_id=org_id, task_id=task_id)
    await session.execute(
        insert(TaskPin)
        .values(user_id=user_id, task_id=task_id, org_id=org_id)
        .on_conflict_do_nothing(index_elements=["user_id", "task_id"])
    )
    pinned_at = (
        await session.execute(
            select(TaskPin.pinned_at).where(TaskPin.user_id == user_id, TaskPin.task_id == task_id)
        )
    ).scalar_one()
    return pinned_at


async def unpin(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, task_id: uuid.UUID
) -> None:
    """Clear the pin. Unpinning what is not pinned is not an error, and it
    works on a task in the bin too: a pin must always be removable."""
    await session.execute(
        delete(TaskPin).where(
            TaskPin.org_id == org_id, TaskPin.user_id == user_id, TaskPin.task_id == task_id
        )
    )


async def pinned_task_ids(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """The person's pins in this workspace, most recently pinned first."""
    rows = await session.execute(
        select(TaskPin.task_id)
        .where(TaskPin.org_id == org_id, TaskPin.user_id == user_id)
        .order_by(TaskPin.pinned_at.desc(), TaskPin.task_id)
    )
    return list(rows.scalars())
