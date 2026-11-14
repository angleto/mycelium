"""The task operations on the registry (ADR-0068), starting with pins.

A pin is a person keeping a task at hand (migration 0027). The pinned list
itself is not an operation of its own: it is the task list with
``pinned=true``, so a pinned task is drawn from the same row every other
list draws, and the filter is one more narrowing of a query that already
exists on both surfaces.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from mycelium_core.operations import Kind, OpCtx, operation
from mycelium_core.services import task_pins as pins_svc

TASKS = "tasks"


class TaskPinIn(BaseModel):
    task_id: uuid.UUID = Field(description="The task to pin or unpin.")


class TaskPinOut(BaseModel):
    task_id: uuid.UUID
    pinned: bool
    pinned_at: dt.datetime | None = None


@operation(
    "tasks.pin",
    domain=TASKS,
    kind=Kind.WRITE,
    access="tasks:write",
    params=TaskPinIn,
    result=TaskPinOut,
)
async def pin(ctx: OpCtx, p: TaskPinIn) -> TaskPinOut:
    """Pin a task for the person this credential acts for: it is listed
    above the recent tasks, first, until it is unpinned, closed or not.
    Changes nothing on the task. Idempotent: pinning a pinned task keeps the
    original pin time. A task in the bin cannot be pinned."""
    pinned_at = await pins_svc.pin(
        ctx.session, org_id=ctx.org_id, user_id=ctx.actor_id, task_id=p.task_id
    )
    return TaskPinOut(task_id=p.task_id, pinned=True, pinned_at=pinned_at)


@operation(
    "tasks.unpin",
    domain=TASKS,
    kind=Kind.WRITE,
    access="tasks:write",
    params=TaskPinIn,
    result=TaskPinOut,
)
async def unpin(ctx: OpCtx, p: TaskPinIn) -> TaskPinOut:
    """Clear the person's pin on a task. Idempotent, and it works on any
    task, closed or in the bin, so a pin can always be removed."""
    await pins_svc.unpin(ctx.session, org_id=ctx.org_id, user_id=ctx.actor_id, task_id=p.task_id)
    return TaskPinOut(task_id=p.task_id, pinned=False)
