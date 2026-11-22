"""The task operations on the registry (ADR-0068): pins and the work digest.

A pin is a person keeping a task at hand (migration 0027). The pinned list
itself is not an operation of its own: it is the task list with
``pinned=true``, so a pinned task is drawn from the same row every other
list draws, and the filter is one more narrowing of a query that already
exists on both surfaces.

The work digest is what happened on a client's or project's tasks in a
window (``services/work_digest``), the material of a progress report.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Self

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from mycelium_core.operations import Kind, OpCtx, operation
from mycelium_core.services import task_pins as pins_svc
from mycelium_core.services import work_digest as digest_svc

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


class WorkDigestIn(BaseModel):
    start_from: AwareDatetime = Field(
        description="Start of the window, inclusive. ISO-8601 with a UTC offset between "
        "-12:00 and +14:00 (2026-11-09T00:00:00+01:00): where a day starts is the reader's "
        "call."
    )
    start_to: AwareDatetime = Field(
        description="End of the window, exclusive, same form. At most 92 calendar days "
        "after start_from, each end read on its own offset's clock, so a quarter across a "
        "daylight-saving change is one call."
    )
    client_tag_id: uuid.UUID | None = Field(
        default=None, description="Only the tasks of this client (its client tag)."
    )
    project_tag_id: uuid.UUID | None = Field(
        default=None, description="Only the tasks of this project (its project tag)."
    )

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.start_to <= self.start_from:
            raise ValueError("start_to must be after start_from")
        if not (
            digest_svc.offset_is_real(self.start_from) and digest_svc.offset_is_real(self.start_to)
        ):
            raise ValueError("each end's UTC offset must be between -12:00 and +14:00")
        if digest_svc.window_days(self.start_from, self.start_to) > digest_svc.MAX_WINDOW:
            raise ValueError(f"the window may span at most {digest_svc.MAX_WINDOW.days} days")
        return self


class DigestEventOut(BaseModel):
    at: dt.datetime
    kind: digest_svc.EventKind
    actor: str | None = None
    via_agent: bool = False
    text: str | None = None
    comment_id: uuid.UUID | None = None
    reply_to: uuid.UUID | None = None
    entry_id: uuid.UUID | None = None
    from_state: str | None = None
    to_state: str | None = None
    duration_seconds: int | None = None
    billable: bool | None = None
    running: bool = False
    count: int = 1


class TaskDigestOut(BaseModel):
    task_id: uuid.UUID
    title: str
    state: str | None = None
    state_is_terminal: bool = False
    is_archived: bool = False
    created_at: dt.datetime
    description: str | None = None
    project_tag_id: uuid.UUID | None = None
    project_name: str | None = None
    client_tag_id: uuid.UUID | None = None
    client_name: str | None = None
    seconds: int
    billable_seconds: int
    events: list[DigestEventOut]


class WorkDigestOut(BaseModel):
    start_from: dt.datetime
    start_to: dt.datetime
    client_tag_id: uuid.UUID | None = None
    project_tag_id: uuid.UUID | None = None
    withheld: list[str]
    seconds: int
    billable_seconds: int
    tasks: list[TaskDigestOut]


@operation(
    "tasks.work_digest",
    domain=TASKS,
    kind=Kind.READ,
    # Any of the three: each section of the answer is then filtered by the
    # key that reads it (``OpCtx.may``), and a section the caller may not
    # read is named in ``withheld`` instead of being returned.
    access=frozenset(s.value for s in digest_svc.Section),
    params=WorkDigestIn,
    result=WorkDigestOut,
)
async def work_digest(ctx: OpCtx, p: WorkDigestIn) -> WorkDigestOut:
    """What happened on the tasks of a client or project in a window: the
    material of a progress report, collected, not summarised.

    Every task with something in ``[start_from, start_to)``, most recent
    activity first, each with its events in order: created, state changes,
    description edits (consecutive ones folded, ``count``), work-diary
    comments in full, and the time entries started in the window with their
    memo. Hours count completed entries only; a running timer is listed
    with ``running``. Tasks in the bin are left out, archived ones kept.
    Without client or project, the whole workspace. Sections the
    credential may not read (tasks, comments, time) are named in
    ``withheld``. To write the report, compose from this and cite each task."""
    readable = frozenset(s for s in digest_svc.Section if ctx.may(s.value))
    d = await digest_svc.digest(
        ctx.session,
        org_id=ctx.org_id,
        start_from=p.start_from,
        start_to=p.start_to,
        client_tag_id=p.client_tag_id,
        project_tag_id=p.project_tag_id,
        readable=readable,
    )
    return WorkDigestOut.model_validate(d, from_attributes=True)
