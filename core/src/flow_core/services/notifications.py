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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, or_, select
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
from flow_core.models.task_collaborator import TaskCollaborator
from flow_core.models.task_tag import TaskTag
from flow_core.models.user import User
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


# Max dispatch attempts before a ``failed`` notification stops being
# revived for retry by a later scan. Bounds the retry of a permanently
# broken target (a bad telegram chat_id, a bouncing address) so it is
# not re-attempted on every tick forever.
MAX_NOTIFICATION_ATTEMPTS = 5


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
    fire_at: dt.datetime | None = None,
) -> Notification:
    """Idempotent by (org, dedupe_key).

    ``fire_at`` is the moment the notification becomes eligible to send
    (``None`` = immediately, for non-reminder notifications). It is
    persisted so ``dispatch_pending`` can HOLD the row until then instead
    of sending it the instant it is enqueued.

    Dedupe is status-aware: a repeated enqueue with the same key returns
    the existing row, EXCEPT that a ``failed`` row still under the attempt
    cap is revived to ``pending`` so a transient send failure is retried
    on the next dispatch. A ``sent`` row is terminal and never re-sent.
    The lookup is org-scoped to match the ``(org_id, dedupe_key)`` unique
    constraint (defence-in-depth on top of RLS)."""
    if dedupe_key is not None:
        existing = (
            await session.execute(
                select(Notification).where(
                    Notification.org_id == org_id,
                    Notification.dedupe_key == dedupe_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Keep the firing moment current (also backfills NULL on rows
            # enqueued before migration 0018 added the column).
            existing.fire_at = fire_at
            if (
                existing.status is NotificationStatus.failed
                and existing.attempts < MAX_NOTIFICATION_ATTEMPTS
            ):
                existing.status = NotificationStatus.pending
                existing.last_error = None
                existing.version += 1
            # Flush so the revived ``pending`` status is visible to a
            # dispatch query later in the same unit of work (the session
            # does not autoflush before reads).
            await session.flush()
            return existing
    n = Notification(
        org_id=org_id,
        user_id=user_id,
        channel=channel,
        kind=kind,
        title=title,
        body=body,
        dedupe_key=dedupe_key,
        fire_at=fire_at,
        status=NotificationStatus.pending,
    )
    try:
        async with session.begin_nested():
            session.add(n)
            await session.flush()
    except IntegrityError:
        return (
            await session.execute(
                select(Notification).where(
                    Notification.org_id == org_id,
                    Notification.dedupe_key == dedupe_key,
                )
            )
        ).scalar_one()
    return n


async def dispatch_pending(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    sender: NotificationSender | None = None,
) -> DispatchResult:
    """Send DUE pending notifications honouring per-user channel prefs;
    one failure never aborts the rest (per-item fault isolation).

    The dispatch gate is ``fire_at IS NULL OR fire_at <= now``: a reminder
    enqueued ahead of time stays pending until its firing moment arrives
    (``fire_at``), and notifications with no firing moment (``NULL``) send
    immediately. A transient send failure bumps ``attempts`` and marks the
    row ``failed``; a later scan revives it for retry under the cap."""
    snd = sender or get_sender()
    now = dt.datetime.now(tz=dt.UTC)
    rows = list(
        (
            await session.execute(
                select(Notification).where(
                    Notification.org_id == org_id,
                    Notification.status == NotificationStatus.pending,
                    or_(
                        Notification.fire_at.is_(None),
                        Notification.fire_at <= now,
                    ),
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
            n.attempts += 1
            failed += 1
            continue
        try:
            await snd.send(channel=n.channel, target=pref.target, title=n.title, body=n.body)
            n.status = NotificationStatus.sent
            n.sent_at = now
            sent += 1
        except Exception as exc:  # channel boundary: isolate per-item
            n.status = NotificationStatus.failed
            n.last_error = str(exc)
            n.attempts += 1
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
    task rows, advance next_run.

    The occurrence copies the template's tags, assignees AND reminder
    offsets, and is anchored on the recurrence's scheduled moment
    (``rec.next_run``): an appointment template (``start_at`` set) spawns
    an appointment at ``next_run`` carrying the template's duration; a
    deadline template (``due_date`` set) spawns a task due at
    ``next_run``. Without this the spawned instance had no firing
    reference and no reminder rows, so a recurring task never fired any
    reminder."""
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
                    select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == rec.task_id)
                )
            )
            .scalars()
            .all()
        )
        # Anchor the occurrence on its scheduled moment so reminders have
        # a firing reference: appointment template -> appointment at
        # next_run (+ duration); deadline template -> due at next_run; a
        # template with neither stays untimed (unchanged behaviour).
        is_appointment = tmpl.start_at is not None
        new_task = await tasks_svc.create_task(
            session,
            org_id=org_id,
            actor_id=actor_id,
            title=tmpl.title,
            description=tmpl.description,
            # ``priority`` is derived from importance x urgency by the
            # service; copy the axes (mandatory since 0102) instead.
            importance=tmpl.importance,
            urgency=tmpl.urgency,
            estimate_effort_h=tmpl.estimate_effort_h,
            executor_kind=tmpl.executor_kind,
            necessity=tmpl.necessity,
            location=tmpl.location,
            monetary_cost=tmpl.monetary_cost,
            budget_id=tmpl.budget_id,
            start_at=rec.next_run if is_appointment else None,
            duration_minutes=tmpl.duration_minutes if is_appointment else None,
            due_date=rec.next_run if (not is_appointment and tmpl.due_date is not None) else None,
            tag_ids=list(tag_ids),
            assignee_ids=list(assignee_ids),
        )
        # Copy the template's reminder offsets onto the occurrence so it
        # fires the same reminders relative to its anchor.
        offsets = (
            (
                await session.execute(
                    select(TaskReminder.offset_minutes).where(TaskReminder.task_id == rec.task_id)
                )
            )
            .scalars()
            .all()
        )
        for off in offsets:
            session.add(TaskReminder(org_id=org_id, task_id=new_task.id, offset_minutes=off))
        rec.last_spawned_at = ref
        rec.next_run = _advance(rec.next_run, rec.freq, rec.interval)
        if rec.until is not None and rec.next_run > rec.until:
            rec.active = False
        spawned += 1
    await session.flush()
    return spawned


def _resolve_tz(name: str | None) -> dt.tzinfo:
    """An IANA timezone name -> tzinfo, falling back to UTC for an unset
    or unrecognised value (a stored ``users.timezone`` should be valid,
    but never let a bad string break the reminder sweep)."""
    if not name:
        return dt.UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return dt.UTC


async def _user_tz(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    cache: dict[uuid.UUID, dt.tzinfo],
) -> dt.tzinfo:
    """The recipient's timezone (``users.timezone``), memoised per sweep.
    Used to render reminder labels in local time and to detect the
    date-only ("no time set") sentinel in the user's own timezone."""
    cached = cache.get(user_id)
    if cached is not None:
        return cached
    name = (
        await session.execute(select(User.timezone).where(User.id == user_id))
    ).scalar_one_or_none()
    tz = _resolve_tz(name)
    cache[user_id] = tz
    return tz


async def scan_reminders(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    within_days: int = 1,
    now: dt.datetime | None = None,
) -> int:
    """Enqueue (idempotent) reminders for assignees with an enabled
    channel. Each task fires its configured ``task_reminders`` N minutes
    before a firing reference:

      * appointment tasks (``start_at`` set) use ``start_at`` with minute
        precision;
      * date-only tasks fall back to ``due_date`` (end-of-day).

    A deadline without explicit reminders gets one implicit reminder at
    the reference. Sub-day offsets (``0 < off < 1440``) only have a
    defined firing minute when the task has a ``start_at``; on date-only
    tasks they are promoted to ``0`` (at reference) so they fire at a
    defined moment rather than silently bucketing to one day before the
    due date (the pre-v2.0.27 behaviour).

    The label shown to the user and the date-only detection are done in
    the RECIPIENT's timezone (``users.timezone``, UTC when unset): the
    SPA stores an unspecified time as end-of-day LOCAL, which round-trips
    to 23:59:59 in that user's timezone (only 23:59:59 UTC for a UTC
    user). The firing moment (``fire_at``) is an absolute instant and is
    timezone-independent."""
    ref = now or dt.datetime.now(tz=dt.UTC)
    # Horizon is "end of the (ref + within_days) calendar day in UTC":
    # since migration 0005 a date-only ``due_date`` lands at 23:59:59
    # UTC, so a simple ``ref + N days`` cutoff would silently exclude
    # tonight's end-of-day deadlines when scan_reminders runs in the
    # early evening. Padding to end-of-day matches the user-facing
    # "within N days" intent ("today + tomorrow, all of them").
    horizon_day = (ref + dt.timedelta(days=within_days)).date()
    horizon = dt.datetime.combine(horizon_day, dt.time(23, 59, 59), tzinfo=dt.UTC)
    candidates = list(
        (
            await session.execute(
                select(Task)
                .join(WorkflowState, WorkflowState.id == Task.state_id)
                .where(
                    Task.deleted_at.is_(None),
                    Task.is_archived.is_(False),
                    WorkflowState.is_terminal.is_(False),
                    or_(
                        Task.start_at.is_not(None),
                        Task.due_date.is_not(None),
                    ),
                )
            )
        )
        .scalars()
        .unique()
        .all()
    )
    tz_cache: dict[uuid.UUID, dt.tzinfo] = {}
    enqueued = 0
    for t in candidates:
        if t.start_at is not None:
            reference: dt.datetime = t.start_at
            is_due_date = False
        elif t.due_date is not None:
            reference = t.due_date
            is_due_date = True
        else:
            continue
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=dt.UTC)
        # Cap how far ahead we look (a 1-week reminder on a task due
        # in 4 months is still in-window once it gets close enough).
        if reference > horizon + dt.timedelta(days=120):
            continue
        offsets = list(
            (
                await session.execute(
                    select(TaskReminder.offset_minutes).where(TaskReminder.task_id == t.id)
                )
            )
            .scalars()
            .all()
        ) or [0]
        assignees = (
            (
                await session.execute(
                    select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == t.id)
                )
            )
            .scalars()
            .all()
        )
        for uid in assignees:
            prefs = [
                p
                for p in await list_prefs(session, org_id=org_id, user_id=uid)
                if p.enabled and p.target
            ]
            if not prefs:
                continue
            tz = await _user_tz(session, user_id=uid, cache=tz_cache)
            local = reference.astimezone(tz)
            # date-only ("no time set") detection in the RECIPIENT's own
            # timezone: the SPA stores an unspecified time as end-of-day
            # local, which round-trips to 23:59:59 in that timezone.
            # Appointment tasks always carry a real time.
            date_only = (
                is_due_date and local.hour == 23 and local.minute == 59 and local.second == 59
            )
            when_label = (
                local.date().isoformat() if date_only else local.strftime("%Y-%m-%d %H:%M %Z")
            )
            for raw_off in offsets:
                # Sub-day offsets on a date-only task have no defined
                # firing minute -> fire at the reference ("at due").
                off = 0 if date_only and 0 < raw_off < 1440 else raw_off
                fire_at = reference - dt.timedelta(minutes=off)
                if fire_at > horizon:
                    continue
                when = "at due" if off == 0 else f"{off} min before"
                for p in prefs:
                    await enqueue(
                        session,
                        org_id=org_id,
                        actor_id=actor_id,
                        user_id=uid,
                        channel=p.channel,
                        kind="reminder",
                        title=f"Task due: {t.title}",
                        body=f"'{t.title}' is due on {when_label} ({when}).",
                        dedupe_key=(
                            f"reminder:{t.id}:{uid}:{p.channel.value}:{fire_at.isoformat()}"
                        ),
                        fire_at=fire_at,
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
