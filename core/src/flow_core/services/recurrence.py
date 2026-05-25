"""Recurrence engine for tasks (migration 0094 ``tasks.recurrence``).

When a task with a ``recurrence`` spec transitions into a terminal
workflow state (typically ``done``), this module spawns the next
occurrence: a fresh task row in the initial workflow state with the
window (``start_at`` for appointments, ``due_date`` for reminders)
shifted forward by the spec, and the rest of the row cloned
(title, priority, assignee, owner, tags, billable, duration, etc.).

Spec format (jsonb on ``tasks.recurrence``):

::

    {
      "kind": "daily" | "weekly" | "monthly" | "yearly",
      "interval": <int, default 1>,
      "by_weekday": ["mon","tue","wed","thu","fri","sat","sun"],
        # weekly only; if omitted, repeats on the anchor's own weekday
      "by_month_day": <1..31>,
        # monthly only; if omitted, repeats on the anchor's own day
      "until": "YYYY-MM-DD"
        # optional end: if the next occurrence would fall after this
        # date, no spawn happens (the chain ends)
    }

Out of scope (RFC 5545 territory): per-occurrence exceptions
(EXDATE), composite RRULE (multiple BYxxx for a single rule),
positional weekdays (the "third Tuesday of the month" form). If a
caller needs them, extend this module rather than free-form the
jsonb -- the recurrence column has no schema enforcement so all
discipline lives here.
"""

from __future__ import annotations

import calendar
import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.identity import Identity
from flow_core.models.task import Task
from flow_core.models.task_collaborator import TaskCollaborator
from flow_core.models.task_participant import TaskParticipant
from flow_core.models.task_tag import TaskTag
from flow_core.models.workflow import WorkflowState
from flow_core.services import workflow as wf_svc

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _validate_spec(spec: dict[str, Any]) -> tuple[str, int]:
    kind = spec.get("kind")
    if kind not in ("daily", "weekly", "monthly", "yearly"):
        raise DomainError(MessageCode.DOMAIN_ERROR)
    interval = int(spec.get("interval", 1))
    if interval < 1:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    if "by_weekday" in spec:
        days = spec["by_weekday"]
        if not isinstance(days, list) or not all(d in _WEEKDAYS for d in days):
            raise DomainError(MessageCode.DOMAIN_ERROR)
    if "by_month_day" in spec:
        dom = spec["by_month_day"]
        if not isinstance(dom, int) or not (1 <= dom <= 31):
            raise DomainError(MessageCode.DOMAIN_ERROR)
    if "until" in spec:
        try:
            dt.date.fromisoformat(spec["until"])
        except (TypeError, ValueError) as exc:
            raise DomainError(MessageCode.DOMAIN_ERROR) from exc
    return kind, interval


def _shift_monthly(anchor: dt.date, months: int, day: int | None = None) -> dt.date:
    """Anchor + N calendar months. If the resulting month does not have
    enough days for ``day`` (or the anchor's day when ``day`` is None),
    clamp to the last day of that month (Feb 31 -> Feb 28/29). Standard
    handling, matches Python ``relativedelta`` and most calendar apps."""
    target_year = anchor.year + (anchor.month - 1 + months) // 12
    target_month = (anchor.month - 1 + months) % 12 + 1
    target_day = day if day is not None else anchor.day
    last_day = calendar.monthrange(target_year, target_month)[1]
    return dt.date(target_year, target_month, min(target_day, last_day))


def next_occurrence_date(anchor: dt.date, spec: dict[str, Any]) -> dt.date | None:
    """Return the next occurrence strictly after ``anchor`` per the
    spec, or None if the chain ends (past ``until``). ``anchor`` is the
    date of the just-completed occurrence."""
    kind, interval = _validate_spec(spec)
    if kind == "daily":
        nxt = anchor + dt.timedelta(days=interval)
    elif kind == "weekly":
        nxt = _next_weekly(anchor, interval, spec.get("by_weekday"))
    elif kind == "monthly":
        nxt = _shift_monthly(anchor, interval, spec.get("by_month_day"))
    else:  # yearly
        try:
            nxt = anchor.replace(year=anchor.year + interval)
        except ValueError:
            # Feb 29 on a non-leap target year: clamp to Feb 28.
            nxt = anchor.replace(year=anchor.year + interval, day=28)
    until = spec.get("until")
    if until and nxt > dt.date.fromisoformat(until):
        return None
    return nxt


def _next_weekly(anchor: dt.date, interval: int, by_weekday: list[str] | None) -> dt.date:
    """Weekly recurrence: if ``by_weekday`` is set, find the next listed
    weekday after the anchor inside the current week; only when none of
    the listed days remain do we jump forward ``interval`` weeks and
    pick the earliest listed day. If ``by_weekday`` is empty / missing,
    simply add ``interval`` weeks."""
    if not by_weekday:
        return anchor + dt.timedelta(weeks=interval)
    target_indices = sorted({_WEEKDAYS.index(d) for d in by_weekday})
    anchor_idx = anchor.weekday()  # Monday=0..Sunday=6
    # Same-week candidates strictly AFTER anchor's weekday.
    later_this_week = [i for i in target_indices if i > anchor_idx]
    if later_this_week:
        return anchor + dt.timedelta(days=later_this_week[0] - anchor_idx)
    # Wrap to the next valid week (anchor's week + interval weeks).
    next_week_monday = anchor + dt.timedelta(days=(7 - anchor_idx) + (interval - 1) * 7)
    return next_week_monday + dt.timedelta(days=target_indices[0])


def next_occurrence_datetime(anchor: dt.datetime, spec: dict[str, Any]) -> dt.datetime | None:
    """Datetime variant for appointment-tasks. Preserves the anchor's
    time-of-day and tzinfo; shifts the date part per the spec."""
    nd = next_occurrence_date(anchor.date(), spec)
    if nd is None:
        return None
    return dt.datetime.combine(nd, anchor.timetz())


async def maybe_spawn_next(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task: Task,
) -> Task | None:
    """Hook: invoked after a task transitions INTO a terminal state.
    No-op if the task has no recurrence. Spawns a sibling row with the
    window shifted forward by the spec and the rest cloned (title,
    priority, assignee, owner, tags, executor_kind, billable,
    duration). Participants are mirrored explicitly so an extra
    invitee carries over; the assignee mirror is handled by the
    0096 trigger on the insert.

    Returns the new ``Task`` or None if the chain ended (past
    ``until``)."""
    if not task.recurrence:
        return None
    spec: dict[str, Any] = task.recurrence
    new_start_at: dt.datetime | None = None
    new_due_date: dt.datetime | None = None
    if task.start_at is not None:
        new_start_at = next_occurrence_datetime(task.start_at, spec)
        if new_start_at is None:
            return None
    elif task.due_date is not None:
        # Migration 0005: due_date carries a time-of-day. Use the
        # datetime variant so we preserve hour/minute on the spawned
        # row (a 18:00 deadline stays 18:00 next occurrence).
        new_due_date = next_occurrence_datetime(task.due_date, spec)
        if new_due_date is None:
            return None
    else:
        # Neither anchor present: nothing to shift. A plain recurring
        # task without a date is a config bug; do not spawn silently.
        return None

    # Reset to the initial state of the same workflow as the
    # just-completed task; otherwise the spawn would inherit "done".
    workflow = await wf_svc.effective_workflow_for_task(session, org_id, task.id)
    initial = (
        await session.execute(
            select(WorkflowState).where(
                WorkflowState.workflow_id == workflow.id,
                WorkflowState.is_initial.is_(True),
            )
        )
    ).scalar_one()

    new_task = Task(
        org_id=org_id,
        title=task.title,
        description=task.description,
        priority=task.priority,
        importance=task.importance,
        urgency=task.urgency,
        start_date=task.start_date,
        due_date=new_due_date if new_start_at is None else task.due_date,
        billable=task.billable,
        state_id=initial.id,
        parent_task_id=task.parent_task_id,
        owner_id=task.owner_id,
        assignee_id=task.assignee_id,
        executor_kind=task.executor_kind,
        estimate_effort_h=task.estimate_effort_h,
        required_capabilities=list(task.required_capabilities or []),
        monetary_cost=task.monetary_cost,
        location=task.location,
        necessity=task.necessity,
        budget_id=task.budget_id,
        created_by_identity_id=task.created_by_identity_id,
        created_by_token_id=task.created_by_token_id,
        start_at=new_start_at,
        duration_minutes=task.duration_minutes if new_start_at is not None else None,
        recurrence=spec,
    )
    session.add(new_task)
    await session.flush()

    # Copy task_tags (project + client + extras) so the spawn keeps
    # the same focus/filter context.
    tag_rows = (
        (await session.execute(select(TaskTag.tag_id).where(TaskTag.task_id == task.id)))
        .scalars()
        .all()
    )
    for tag_id in tag_rows:
        session.add(TaskTag(org_id=org_id, task_id=new_task.id, tag_id=tag_id))

    # Copy task_collaborators (the M:N "extra hands" set; the singular
    # assignee is on tasks.assignee_id and already copied above).
    collab_rows = (
        (
            await session.execute(
                select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == task.id)
            )
        )
        .scalars()
        .all()
    )
    for uid in collab_rows:
        session.add(TaskCollaborator(org_id=org_id, task_id=new_task.id, user_id=uid))

    # Copy EXTRA participants only (not the assignee mirror -- the 0096
    # trigger already inserted that one on the new_task insert above).
    if new_start_at is not None:
        extras = (
            (
                await session.execute(
                    select(TaskParticipant.identity_id)
                    .join(Identity, Identity.id == TaskParticipant.identity_id)
                    .where(
                        TaskParticipant.task_id == task.id,
                        TaskParticipant.identity_id != task.assignee_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for ident_id in extras:
            session.add(
                TaskParticipant(
                    org_id=org_id,
                    task_id=new_task.id,
                    identity_id=ident_id,
                    start_at=new_start_at,
                    duration_minutes=task.duration_minutes,
                )
            )

    await session.flush()
    # Unused but bound to keep the actor in scope for future audit
    # extensions (the spawn itself is implicitly attributable to the
    # state-transition actor; no extra audit row for now since the
    # source set_state audit captures intent).
    _ = actor_id
    return new_task
