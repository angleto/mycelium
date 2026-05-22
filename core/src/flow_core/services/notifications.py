"""Notifications, per-user channel prefs, recurring tasks, reminders
(FR-12).

Notifications are idempotent per (org, dedupe_key); dispatch is
fault-isolated (one failure never aborts the batch). Recurring task
instances are independent task rows (no shared state); recurrence and
dependencies are mutually exclusive in v1. Reminder scans are
idempotent via a stable dedupe key.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.dependency import TaskDependency
from flow_core.models.membership import Role
from flow_core.models.notification import (
    Notification,
    NotificationChannelKind,
    NotificationPref,
    NotificationStatus,
    RecurrenceFreq,
    TaskRecurrence,
    TaskReminder,
)
from flow_core.models.task import Task
from flow_core.models.task_assignee import TaskAssignee
from flow_core.models.task_tag import TaskTag
from flow_core.models.workflow import WorkflowState
from flow_core.notification_channel import NotificationSender, get_sender
from flow_core.services import audit
from flow_core.services import tasks as tasks_svc
from flow_core.services.rbac import require_role


@dataclass(frozen=True)
class DispatchResult:
    sent: int
    failed: int


async def set_pref(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    channel: NotificationChannelKind,
    enabled: bool,
    target: str,
) -> NotificationPref:
    await require_role(session, org_id, actor_id, Role.member)
    pref = (
        await session.execute(
            select(NotificationPref).where(
                NotificationPref.org_id == org_id,
                NotificationPref.user_id == user_id,
                NotificationPref.channel == channel,
            )
        )
    ).scalar_one_or_none()
    if pref is None:
        pref = NotificationPref(
            org_id=org_id,
            user_id=user_id,
            channel=channel,
            enabled=enabled,
            target=target,
        )
        session.add(pref)
    else:
        pref.enabled = enabled
        pref.target = target
        pref.version += 1
    await session.flush()
    return pref


async def list_prefs(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[NotificationPref]:
    return list(
        (
            await session.execute(
                select(NotificationPref).where(
                    NotificationPref.org_id == org_id,
                    NotificationPref.user_id == user_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def enqueue(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    user_id: uuid.UUID,
    channel: NotificationChannelKind,
    kind: str,
    title: str,
    body: str,
    dedupe_key: str | None = None,
) -> Notification:
    """Idempotent by (org, dedupe_key): a repeated enqueue with the
    same key returns the existing notification."""
    if dedupe_key is not None:
        existing = (
            await session.execute(select(Notification).where(Notification.dedupe_key == dedupe_key))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    n = Notification(
        org_id=org_id,
        user_id=user_id,
        channel=channel,
        kind=kind,
        title=title,
        body=body,
        dedupe_key=dedupe_key,
        status=NotificationStatus.pending,
    )
    try:
        async with session.begin_nested():
            session.add(n)
            await session.flush()
    except IntegrityError:
        return (
            await session.execute(select(Notification).where(Notification.dedupe_key == dedupe_key))
        ).scalar_one()
    return n


async def dispatch_pending(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    sender: NotificationSender | None = None,
) -> DispatchResult:
    """Send all pending notifications honouring per-user channel prefs;
    one failure never aborts the rest (per-item fault isolation)."""
    snd = sender or get_sender()
    rows = list(
        (
            await session.execute(
                select(Notification).where(
                    Notification.org_id == org_id,
                    Notification.status == NotificationStatus.pending,
                )
            )
        )
        .scalars()
        .all()
    )
    sent = failed = 0
    for n in rows:
        pref = (
            await session.execute(
                select(NotificationPref).where(
                    NotificationPref.org_id == org_id,
                    NotificationPref.user_id == n.user_id,
                    NotificationPref.channel == n.channel,
                )
            )
        ).scalar_one_or_none()
        if pref is None or not pref.enabled or not pref.target:
            n.status = NotificationStatus.failed
            n.last_error = "no enabled channel pref"
            failed += 1
            continue
        try:
            await snd.send(channel=n.channel, target=pref.target, title=n.title, body=n.body)
            n.status = NotificationStatus.sent
            n.sent_at = dt.datetime.now(tz=dt.UTC)
            sent += 1
        except Exception as exc:  # channel boundary: isolate per-item
            n.status = NotificationStatus.failed
            n.last_error = str(exc)
            failed += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="notification",
        entity_id=None,
        action="dispatch",
        diff={"sent": str(sent), "failed": str(failed)},
    )
    return DispatchResult(sent=sent, failed=failed)


# --- recurring tasks ---


def _advance(when: dt.datetime, freq: RecurrenceFreq, interval: int) -> dt.datetime:
    if freq is RecurrenceFreq.daily:
        return when + dt.timedelta(days=interval)
    if freq is RecurrenceFreq.weekly:
        return when + dt.timedelta(weeks=interval)
    # monthly / yearly: add N calendar months (yearly = interval*12),
    # clamp the day to the target month length.
    months = interval * 12 if freq is RecurrenceFreq.yearly else interval
    month0 = when.month - 1 + months
    year = when.year + month0 // 12
    month = month0 % 12 + 1
    day = min(
        when.day,
        [
            31,
            29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31,
            30,
            31,
            30,
            31,
            31,
            30,
            31,
            30,
            31,
        ][month - 1],
    )
    return when.replace(year=year, month=month, day=day)


async def create_recurrence(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    freq: RecurrenceFreq,
    next_run: dt.datetime,
    interval: int = 1,
    until: dt.datetime | None = None,
) -> TaskRecurrence:
    await require_role(session, org_id, actor_id, Role.member)
    await tasks_svc.get_task(session, org_id=org_id, task_id=task_id)
    has_dep = (
        await session.execute(
            select(TaskDependency.id)
            .where(
                (TaskDependency.predecessor_id == task_id)
                | (TaskDependency.successor_id == task_id)
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if has_dep is not None:
        raise DomainError(MessageCode.RECURRENCE_WITH_DEPS)
    rec = (
        await session.execute(select(TaskRecurrence).where(TaskRecurrence.task_id == task_id))
    ).scalar_one_or_none()
    if rec is None:
        rec = TaskRecurrence(
            task_id=task_id,
            org_id=org_id,
            freq=freq,
            interval=interval,
            next_run=next_run,
            until=until,
            active=True,
        )
        session.add(rec)
    else:
        rec.freq = freq
        rec.interval = interval
        rec.next_run = next_run
        rec.until = until
        rec.active = True
        rec.version += 1
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task_recurrence",
        entity_id=task_id,
        action="create",
    )
    return rec


async def spawn_due(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    now: dt.datetime | None = None,
) -> int:
    """Deterministically materialize due recurrences as new independent
    task rows (template's tags + assignees copied), advance next_run."""
    ref = now or dt.datetime.now(tz=dt.UTC)
    recs = list(
        (
            await session.execute(
                select(TaskRecurrence).where(
                    TaskRecurrence.active.is_(True),
                    TaskRecurrence.next_run <= ref,
                )
            )
        )
        .scalars()
        .all()
    )
    recs.sort(key=lambda r: (r.next_run, str(r.task_id)))
    spawned = 0
    for rec in recs:
        if rec.until is not None and rec.next_run > rec.until:
            rec.active = False
            continue
        tmpl = await tasks_svc.get_task(session, org_id=org_id, task_id=rec.task_id)
        tag_ids = (
            (await session.execute(select(TaskTag.tag_id).where(TaskTag.task_id == rec.task_id)))
            .scalars()
            .all()
        )
        assignee_ids = (
            (
                await session.execute(
                    select(TaskAssignee.user_id).where(TaskAssignee.task_id == rec.task_id)
                )
            )
            .scalars()
            .all()
        )
        await tasks_svc.create_task(
            session,
            org_id=org_id,
            actor_id=actor_id,
            title=tmpl.title,
            description=tmpl.description,
            priority=tmpl.priority,
            estimate_effort_h=tmpl.estimate_effort_h,
            executor_kind=tmpl.executor_kind,
            necessity=tmpl.necessity,
            location=tmpl.location,
            monetary_cost=tmpl.monetary_cost,
            budget_id=tmpl.budget_id,
            tag_ids=list(tag_ids),
            assignee_ids=list(assignee_ids),
        )
        rec.last_spawned_at = ref
        rec.next_run = _advance(rec.next_run, rec.freq, rec.interval)
        if rec.until is not None and rec.next_run > rec.until:
            rec.active = False
        spawned += 1
    await session.flush()
    return spawned


async def scan_reminders(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    within_days: int = 1,
    now: dt.datetime | None = None,
) -> int:
    """Enqueue (idempotent) reminders for assignees with an enabled
    channel. Each task fires its configured ``task_reminders`` (N
    offsets before due_date, Google-Calendar style); a task with a
    due_date but no reminders gets one implicit reminder at due."""
    ref = now or dt.datetime.now(tz=dt.UTC)
    horizon = (ref + dt.timedelta(days=within_days)).date()
    # Consider tasks due far enough out that an early reminder could
    # already be in-window (cap the lead we look back at ~120 days).
    candidates = list(
        (
            await session.execute(
                select(Task)
                .join(WorkflowState, WorkflowState.id == Task.state_id)
                .where(
                    Task.deleted_at.is_(None),
                    Task.is_archived.is_(False),
                    WorkflowState.is_terminal.is_(False),
                    Task.due_date.is_not(None),
                    Task.due_date <= horizon + dt.timedelta(days=120),
                )
            )
        )
        .scalars()
        .unique()
        .all()
    )
    enqueued = 0
    for t in candidates:
        offsets = list(
            (
                await session.execute(
                    select(TaskReminder.offset_minutes).where(TaskReminder.task_id == t.id)
                )
            )
            .scalars()
            .all()
        ) or [0]
        due = t.due_date
        if due is None:
            continue
        assignees = (
            (
                await session.execute(
                    select(TaskAssignee.user_id).where(TaskAssignee.task_id == t.id)
                )
            )
            .scalars()
            .all()
        )
        for off in offsets:
            fire_date = due - dt.timedelta(days=-(-off // 1440))
            if fire_date > horizon:
                continue
            when = "at due" if off == 0 else f"{off} min before"
            for uid in assignees:
                prefs = await list_prefs(session, org_id=org_id, user_id=uid)
                for p in prefs:
                    if not p.enabled or not p.target:
                        continue
                    await enqueue(
                        session,
                        org_id=org_id,
                        actor_id=actor_id,
                        user_id=uid,
                        channel=p.channel,
                        kind="reminder",
                        title=f"Task due: {t.title}",
                        body=f"'{t.title}' is due on {due} ({when}).",
                        dedupe_key=(f"reminder:{t.id}:{uid}:{p.channel.value}:{due}:{off}"),
                    )
                    enqueued += 1
    return enqueued


async def list_reminders(
    session: AsyncSession, *, org_id: uuid.UUID, task_id: uuid.UUID
) -> list[TaskReminder]:
    return list(
        (
            await session.execute(
                select(TaskReminder)
                .where(TaskReminder.task_id == task_id)
                .order_by(TaskReminder.offset_minutes)
            )
        )
        .scalars()
        .all()
    )


async def add_reminder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    offset_minutes: int,
) -> TaskReminder:
    await require_role(session, org_id, actor_id, Role.member)
    await tasks_svc.get_task(session, org_id=org_id, task_id=task_id)
    existing = (
        await session.execute(
            select(TaskReminder).where(
                TaskReminder.task_id == task_id,
                TaskReminder.offset_minutes == offset_minutes,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    r = TaskReminder(org_id=org_id, task_id=task_id, offset_minutes=max(0, offset_minutes))
    session.add(r)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="add_reminder",
    )
    return r


async def remove_reminder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    reminder_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    await session.execute(
        delete(TaskReminder).where(TaskReminder.id == reminder_id, TaskReminder.task_id == task_id)
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="task",
        entity_id=task_id,
        action="remove_reminder",
    )


async def list_notifications(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    status: NotificationStatus | None = None,
) -> Sequence[Notification]:
    stmt = select(Notification)
    if user_id is not None:
        stmt = stmt.where(Notification.user_id == user_id)
    if status is not None:
        stmt = stmt.where(Notification.status == status)
    stmt = stmt.order_by(Notification.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def delete_notification(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> None:
    """Dismiss a notification from the actor's log. Scoped to the actor's
    own rows (a member can only clear notifications addressed to them)."""
    n = (
        await session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.org_id == org_id,
                Notification.user_id == actor_id,
            )
        )
    ).scalar_one_or_none()
    if n is None:
        raise DomainError(MessageCode.NOTIFICATION_NOT_FOUND)
    await session.delete(n)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="notification",
        entity_id=notification_id,
        action="delete",
    )
