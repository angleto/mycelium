"""What happened on a client's or a project's tasks between two instants.

The material for a progress report (a weekly status meeting, say): every task
of the scope that had something happen to it in the window, with the events
that happened, in order. It is a COLLECTION and nothing more. It does not
summarise, rank by importance or rewrite anything: the prose is composed by
whoever reads it, a person or a language model in front of that person, with
this as its source. A summary generated and stored here would go stale the
moment a comment is added after it, and nothing here could check it.

Where each kind of event comes from, and why that source:

- ``task_created``, ``state_changed``, ``description_edited``: the activity
  log, which is append-only and kept indefinitely. Not ``entity_revision``,
  whose rows are purged after the retention window: a report on an older
  window would lose its transitions silently. A ``set_state`` recorded before
  the log carried the states it moved between has neither, and is reported
  with both left empty rather than guessed from a revision.
- ``comment``: the task's work-diary comments (``doc_kind='task_description'``,
  ``kind='comment'``) created in the window and not deleted. Suggestions are
  proposed edits to the description, not entries of the diary, and are left out.
- ``time_entry``: entries STARTED in the window, the same rule ``GET
  /time/entries`` and every time report apply, so the hours here and there
  agree. A running timer is reported with ``running`` and no duration, and is
  not counted in the totals, again as the reports do.

The window is half-open, ``[start_from, start_to)``, and both ends must carry
a UTC offset: a day boundary is a property of the reader's timezone, and the
server has no business choosing it.

What a caller sees follows what it may read: task events need ``tasks:read``,
comments ``comments:read``, time entries ``time:read``. A section the caller
may not read is omitted and named in ``withheld``, and a task with no event
the caller may see is not listed at all, so its title is not disclosed through
this door either.

Tasks in the bin are left out: what was thrown away is not reported as work.
Archived tasks are kept: archiving is filing, not retracting.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.models.activity_log import ActivityLog
from mycelium_core.models.annotation import Annotation
from mycelium_core.models.task import ExecKind, Task
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import caller as caller_svc
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import time_tracking as time_svc

#: The longest window one call may cover. A quarter, so a quarterly report is
#: one call; the answer carries every comment of the window in full, and the
#: bound keeps that proportional to a period somebody reports on rather than
#: to the age of the workspace.
MAX_WINDOW = dt.timedelta(days=92)


#: Activity-log actions on a task that the digest reads, and nothing else:
#: an assignment, a reschedule or a priority edit is not progress.
_TASK_ACTIONS = (
    "create",
    "set_state",
    "update",
    "description.append",
    "description.prepend",
    "description.replace",
)


class Section(enum.StrEnum):
    """A part of the digest and the scope key that reads it."""

    tasks = "tasks:read"
    comments = "comments:read"
    time = "time:read"


class EventKind(enum.StrEnum):
    task_created = "task_created"
    state_changed = "state_changed"
    description_edited = "description_edited"
    comment = "comment"
    time_entry = "time_entry"


@dataclass(frozen=True, slots=True)
class DigestEvent:
    at: dt.datetime
    kind: EventKind
    actor: str | None = None
    #: The actor is a user's agent (an MCP token, an agent run) rather than
    #: the user in person.
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
    #: Consecutive description edits by the same actor, with nothing else in
    #: between, are one event: an editor that saves as you type would
    #: otherwise bury the diary under them. ``count`` says how many.
    count: int = 1


@dataclass(slots=True)
class TaskDigest:
    task_id: uuid.UUID
    title: str
    state: str | None
    state_is_terminal: bool
    is_archived: bool
    created_at: dt.datetime
    description: str | None
    project_tag_id: uuid.UUID | None
    project_name: str | None
    client_tag_id: uuid.UUID | None
    client_name: str | None
    seconds: int = 0
    billable_seconds: int = 0
    events: list[DigestEvent] = field(default_factory=list)

    @property
    def last_event_at(self) -> dt.datetime:
        return self.events[-1].at


@dataclass(frozen=True, slots=True)
class WorkDigest:
    start_from: dt.datetime
    start_to: dt.datetime
    client_tag_id: uuid.UUID | None
    project_tag_id: uuid.UUID | None
    withheld: list[str]
    seconds: int
    billable_seconds: int
    tasks: list[TaskDigest]


def _scoped_tasks(
    org_id: uuid.UUID, client_tag_id: uuid.UUID | None, project_tag_id: uuid.UUID | None
) -> Select[tuple[uuid.UUID]]:
    stmt = select(Task.id).where(Task.org_id == org_id, Task.deleted_at.is_(None))
    for tag_id in (client_tag_id, project_tag_id):
        if tag_id is not None:
            stmt = stmt.where(Task.id.in_(time_svc.tasks_carrying(tag_id)))
    return stmt


async def _task_events(
    session: AsyncSession,
    org_id: uuid.UUID,
    scope: Select[tuple[uuid.UUID]],
    start_from: dt.datetime,
    start_to: dt.datetime,
) -> list[tuple[uuid.UUID, DigestEvent, uuid.UUID | None]]:
    """``(task_id, event, actor_user_id)``; the handle is filled in later in
    one batch with the others."""
    rows = (
        await session.execute(
            select(ActivityLog)
            .where(
                # org_id first: it leads ix_activity_log_org_entity_ts, which
                # with entity and the ts range is the whole access path.
                ActivityLog.org_id == org_id,
                ActivityLog.entity == "task",
                ActivityLog.entity_id.in_(scope),
                ActivityLog.action.in_(_TASK_ACTIONS),
                ActivityLog.ts >= start_from,
                ActivityLog.ts < start_to,
            )
            .order_by(ActivityLog.ts, ActivityLog.id)
        )
    ).scalars()
    state_ids: set[uuid.UUID] = set()
    raw: list[tuple[ActivityLog, EventKind]] = []
    for row in rows:
        diff = row.diff or {}
        if row.action == "create":
            kind = EventKind.task_created
        elif row.action == "set_state" or (row.action == "update" and "state_id" in diff):
            if diff.get("from_state_id") is not None and diff.get("from_state_id") == diff.get(
                "state_id"
            ):
                continue  # re-entering the same state is not a transition
            kind = EventKind.state_changed
            for key in ("from_state_id", "state_id"):
                if diff.get(key):
                    state_ids.add(uuid.UUID(str(diff[key])))
        elif row.action.startswith("description.") or _touches_description(diff):
            kind = EventKind.description_edited
        else:
            continue
        raw.append((row, kind))

    names: dict[uuid.UUID, str] = {}
    if state_ids:
        names = {
            sid: name
            for sid, name in (
                await session.execute(
                    select(WorkflowState.id, WorkflowState.name).where(
                        WorkflowState.id.in_(state_ids)
                    )
                )
            ).all()
        }

    def _state(value: object) -> str | None:
        return names.get(uuid.UUID(str(value))) if value else None

    out: list[tuple[uuid.UUID, DigestEvent, uuid.UUID | None]] = []
    for row, kind in raw:
        diff = row.diff or {}
        if row.entity_id is None:  # narrows the type: the query matched it to a task id
            continue
        out.append(
            (
                row.entity_id,
                DigestEvent(
                    at=row.ts,
                    kind=kind,
                    # A model made the change: an agent, or a Telegram turn.
                    via_agent=caller_svc.classify(row.actor_kind, row.actor_subject_id).is_model,
                    from_state=_state(diff.get("from_state_id"))
                    if kind is EventKind.state_changed
                    else None,
                    to_state=_state(diff.get("state_id"))
                    if kind is EventKind.state_changed
                    else None,
                ),
                row.actor_id,
            )
        )
    return out


async def _comment_events(
    session: AsyncSession,
    org_id: uuid.UUID,
    scope: Select[tuple[uuid.UUID]],
    start_from: dt.datetime,
    start_to: dt.datetime,
) -> list[tuple[uuid.UUID, DigestEvent]]:
    rows = (
        await session.execute(
            select(Annotation)
            .where(
                Annotation.org_id == org_id,
                Annotation.doc_kind == "task_description",
                Annotation.kind == "comment",
                Annotation.task_id.in_(scope),
                Annotation.deleted_at.is_(None),
                Annotation.created_at >= start_from,
                Annotation.created_at < start_to,
            )
            .order_by(Annotation.created_at, Annotation.id)
        )
    ).scalars()
    comments = list(rows)
    handles = await identities_svc.handles_for_identities(
        session,
        org_id=org_id,
        identity_ids=[r.author_identity_id for r in comments if r.author_identity_id is not None],
    )
    out: list[tuple[uuid.UUID, DigestEvent]] = []
    for r in comments:
        if r.task_id is None:  # narrows the type: ck_comments_doc_kind requires it
            continue
        out.append(
            (
                r.task_id,
                DigestEvent(
                    at=r.created_at,
                    kind=EventKind.comment,
                    actor=handles.get(r.author_identity_id) if r.author_identity_id else None,
                    text=r.body,
                    comment_id=r.id,
                    reply_to=r.parent_id,
                ),
            )
        )
    return out


async def _time_events(
    session: AsyncSession,
    org_id: uuid.UUID,
    client_tag_id: uuid.UUID | None,
    project_tag_id: uuid.UUID | None,
    start_from: dt.datetime,
    start_to: dt.datetime,
) -> list[tuple[uuid.UUID, DigestEvent, uuid.UUID]]:
    entries = await time_svc.list_entries(
        session,
        org_id=org_id,
        start_from=start_from,
        start_to=start_to,
        client_tag_id=client_tag_id,
        project_tag_id=project_tag_id,
    )
    return [
        (
            e.task_id,
            DigestEvent(
                at=e.started_at,
                kind=EventKind.time_entry,
                via_agent=e.executor_kind is not ExecKind.human,
                text=e.memo,
                entry_id=e.id,
                duration_seconds=e.duration_seconds,
                billable=e.billable,
                running=e.ended_at is None,
            ),
            e.user_id,
        )
        for e in entries
    ]


def _touches_description(diff: dict[str, object]) -> bool:
    """An ``update`` that wrote the description. Two shapes are on record: an
    edit logs the written values (``{"description": ...}``), an accepted
    suggestion logs ``{"via": "suggestion_accept", "fields": "description"}``."""
    return "description" in diff or diff.get("fields") == "description"


def _fold_description_edits(events: Sequence[DigestEvent]) -> list[DigestEvent]:
    out: list[DigestEvent] = []
    for ev in events:
        prev = out[-1] if out else None
        if (
            prev is not None
            and ev.kind is EventKind.description_edited
            and prev.kind is EventKind.description_edited
            and prev.actor == ev.actor
            and prev.via_agent == ev.via_agent
        ):
            out[-1] = DigestEvent(
                at=ev.at,
                kind=prev.kind,
                actor=prev.actor,
                via_agent=prev.via_agent,
                count=prev.count + 1,
            )
            continue
        out.append(ev)
    return out


async def digest(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    start_from: dt.datetime,
    start_to: dt.datetime,
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
    readable: frozenset[Section] = frozenset(Section),
) -> WorkDigest:
    """The digest of ``[start_from, start_to)`` over the tasks carrying
    ``client_tag_id`` and/or ``project_tag_id`` (every task of the workspace
    when neither is given), restricted to the ``readable`` sections.

    The window is validated by the caller's argument model; this function
    trusts it."""
    scope = _scoped_tasks(org_id, client_tag_id, project_tag_id)

    by_task: dict[uuid.UUID, list[DigestEvent]] = {}
    pending: list[tuple[uuid.UUID, DigestEvent, uuid.UUID | None]] = []
    seconds: dict[uuid.UUID, list[int]] = {}

    if Section.tasks in readable:
        pending.extend(await _task_events(session, org_id, scope, start_from, start_to))
    if Section.time in readable:
        for task_id, ev, user_id in await _time_events(
            session, org_id, client_tag_id, project_tag_id, start_from, start_to
        ):
            pending.append((task_id, ev, user_id))
            if ev.duration_seconds is not None:
                acc = seconds.setdefault(task_id, [0, 0])
                acc[0] += ev.duration_seconds
                if ev.billable:
                    acc[1] += ev.duration_seconds
    if Section.comments in readable:
        for task_id, ev in await _comment_events(session, org_id, scope, start_from, start_to):
            by_task.setdefault(task_id, []).append(ev)

    user_ids = {u for _, _, u in pending if u is not None}
    handles = await identities_svc.handles_for_users(session, user_ids=list(user_ids))
    for owner_task, event, actor_user in pending:
        actor = handles.get(actor_user) if actor_user is not None else None
        by_task.setdefault(owner_task, []).append(
            dataclasses.replace(event, actor=actor) if actor else event
        )

    # The time entries come from ``list_entries``, which does not know about
    # the bin; drop what the task scope excludes so every listed task is one
    # the other sections could have listed too.
    live = set((await session.execute(scope.where(Task.id.in_(list(by_task))))).scalars())
    task_ids = [t for t in by_task if t in live]

    tasks: list[TaskDigest] = []
    if task_ids:
        rows = (
            await session.execute(
                select(Task, WorkflowState.name, WorkflowState.is_terminal)
                .join(WorkflowState, WorkflowState.id == Task.state_id, isouter=True)
                .where(Task.id.in_(task_ids))
            )
        ).all()
        ctxs = await time_svc.resolve_task_contexts(session, task_ids)
        show_task = Section.tasks in readable
        for task, state_name, is_terminal in rows:
            ctx = ctxs.get(task.id)
            events = sorted(by_task[task.id], key=lambda e: e.at)
            acc = seconds.get(task.id, [0, 0])
            tasks.append(
                TaskDigest(
                    task_id=task.id,
                    title=task.title,
                    state=state_name if show_task else None,
                    state_is_terminal=bool(is_terminal) if show_task else False,
                    is_archived=task.is_archived if show_task else False,
                    created_at=task.created_at,
                    description=task.description if show_task else None,
                    project_tag_id=ctx.project_tag_id if ctx else None,
                    project_name=ctx.project_name if ctx else None,
                    client_tag_id=ctx.client_tag_id if ctx else None,
                    client_name=ctx.client_name if ctx else None,
                    seconds=acc[0],
                    billable_seconds=acc[1],
                    events=_fold_description_edits(events),
                )
            )
    # Most recent activity first: what a report opens with is what is moving.
    tasks.sort(key=lambda t: (t.last_event_at, str(t.task_id)), reverse=True)

    return WorkDigest(
        start_from=start_from,
        start_to=start_to,
        client_tag_id=client_tag_id,
        project_tag_id=project_tag_id,
        withheld=sorted(s.value for s in Section if s not in readable),
        seconds=sum(t.seconds for t in tasks),
        billable_seconds=sum(t.billable_seconds for t in tasks),
        tasks=tasks,
    )
