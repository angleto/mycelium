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
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError
from mycelium_core.i18n import DEFAULT_LOCALE, MessageCode, render
from mycelium_core.models.dependency import TaskDependency
from mycelium_core.models.identity import Identity, IdentityKind
from mycelium_core.models.membership import Membership, Role
from mycelium_core.models.notification import (
    Notification,
    NotificationChannelKind,
    NotificationPref,
    NotificationStatus,
    RecurrenceFreq,
    TaskRecurrence,
    TaskReminder,
)
from mycelium_core.models.push_subscription import PushSubscription
from mycelium_core.models.task import Task
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.models.user import User
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.notification_channel import NotificationSender, get_sender
from mycelium_core.services import audit
from mycelium_core.services import recurrence as recurrence_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.notifications_webpush import WebPushGone
from mycelium_core.services.rbac import require_role
from mycelium_core.timewindow import DEFAULT_DAY_START_MINUTE, day_start_anchor, resolve_tz

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchResult:
    sent: int
    failed: int
    # Pending notifications dropped at send time because their task is no
    # longer eligible (terminal / archived / soft-deleted). Neither sent
    # nor failed: the row is deleted rather than fired late.
    suppressed: int = 0


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
    task_id: uuid.UUID | None = None,
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
            # Keep the task linkage current too (backfills NULL on rows
            # enqueued before migration 0048 added the column) so the
            # dispatch-time eligibility gate sees it.
            existing.task_id = task_id
            # Refresh the content of a not-yet-sent row so a re-scan picks up
            # an improved title/body (e.g. the added task deep-link, dropped
            # redundant text) in place, instead of leaving a stale message
            # queued. A ``sent`` row is terminal and never rewritten.
            if existing.status is not NotificationStatus.sent:
                existing.title = title
                existing.body = body
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
        task_id=task_id,
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


async def _dispatch_webpush(
    session: AsyncSession,
    sender: NotificationSender,
    n: Notification,
    *,
    org_id: uuid.UUID,
    now: dt.datetime,
) -> bool:
    """Fan a webpush notification out to all of the user's subscriptions.

    Unlike email/telegram the pref carries no ``target`` (the targets are
    the device subscriptions): the pref is just the on/off switch. Succeeds
    if at least one device accepts; an endpoint the push service reports
    gone (404/410) is pruned. Mutates ``n`` in place; returns whether it
    counts as sent."""
    pref = (
        await session.execute(
            select(NotificationPref).where(
                NotificationPref.org_id == org_id,
                NotificationPref.user_id == n.user_id,
                NotificationPref.channel == NotificationChannelKind.webpush,
            )
        )
    ).scalar_one_or_none()
    if pref is None or not pref.enabled:
        n.status = NotificationStatus.failed
        n.last_error = "no enabled channel pref"
        n.attempts += 1
        return False
    subs = list(
        (
            await session.execute(
                select(PushSubscription).where(
                    PushSubscription.org_id == org_id,
                    PushSubscription.user_id == n.user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not subs:
        n.status = NotificationStatus.failed
        n.last_error = "no webpush subscriptions"
        n.attempts += 1
        return False
    delivered = 0
    last_error = ""
    for sub in subs:
        target = json.dumps(
            {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
        )
        try:
            await sender.send(
                channel=NotificationChannelKind.webpush,
                target=target,
                title=n.title,
                body=n.body,
            )
            delivered += 1
        except WebPushGone:
            await session.delete(sub)  # endpoint permanently gone -> prune
        except Exception as exc:  # transient: keep the row, record + retry later
            last_error = str(exc)
    if delivered > 0:
        n.status = NotificationStatus.sent
        n.sent_at = now
        return True
    n.status = NotificationStatus.failed
    n.attempts += 1
    n.last_error = last_error or "no live webpush subscriptions"
    return False


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
    # Re-validate task eligibility at SEND time. ``scan_reminders`` excludes
    # terminal / archived / soft-deleted tasks only at ENQUEUE time; a
    # reminder enqueued earlier and HELD until its ``fire_at`` would
    # otherwise fire even though its task has since closed. This is the
    # single chokepoint that also catches paths a per-transition hook would
    # miss: archiving, soft-deleting, or retroactively flipping a state's
    # ``is_terminal`` flag (which never calls ``set_state``). Notifications
    # with no ``task_id`` (coordination offers/handoffs) are never gated.
    gated_task_ids = {n.task_id for n in rows if n.task_id is not None}
    ineligible_task_ids: set[uuid.UUID] = set()
    if gated_task_ids:
        ineligible_task_ids = set(
            (
                await session.execute(
                    select(Task.id)
                    .join(WorkflowState, WorkflowState.id == Task.state_id)
                    .where(
                        Task.id.in_(gated_task_ids),
                        or_(
                            Task.deleted_at.is_not(None),
                            Task.is_archived.is_(True),
                            WorkflowState.is_terminal.is_(True),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    sent = failed = suppressed = 0
    for n in rows:
        if n.task_id is not None and n.task_id in ineligible_task_ids:
            # Task no longer eligible: drop the queued notification instead
            # of firing it late. Deleting (vs. holding) is safe because a
            # later scan re-creates the reminder only if the task returns to
            # an active state and the moment is still in window.
            await session.delete(n)
            suppressed += 1
            continue
        # webpush has no single per-pref target: fan out to every device.
        if n.channel == NotificationChannelKind.webpush:
            if await _dispatch_webpush(session, snd, n, org_id=org_id, now=now):
                sent += 1
            else:
                failed += 1
            continue
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
        diff={"sent": str(sent), "failed": str(failed), "suppressed": str(suppressed)},
    )
    return DispatchResult(sent=sent, failed=failed, suppressed=suppressed)


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
    reminder.

    Each occurrence is materialised inside its own SAVEPOINT, so one
    template that cannot spawn never rolls back the rest of the sweep;
    its schedule is advanced anyway, otherwise the drain would retry
    the same failing spawn forever."""
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
        # Flush the schedule bookkeeping of the previous iterations
        # BEFORE this one opens its SAVEPOINT: rolling a SAVEPOINT back
        # expires every state still dirty in the session, so an
        # un-flushed ``next_run`` advance would be silently reverted and
        # its occurrence spawned twice on the next sweep.
        await session.flush()
        if rec.until is not None and rec.next_run > rec.until:
            rec.active = False
            continue
        tmpl = await tasks_svc.get_task(session, org_id=org_id, task_id=rec.task_id)
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
        # The tags the template carries are NOT copied verbatim: the
        # occurrence's client/project pair is resolved by the
        # choke-point inside ``create_task`` (docs/adr/0003), and
        # ``spawn_tag_ids`` hands it an already-normalised bag so a
        # template that drifted before the invariant landed does not
        # reject every occurrence, once per sweep, forever.
        tag_ids = await recurrence_svc.spawn_tag_ids(session, template_task_id=rec.task_id)
        try:
            # Per-occurrence SAVEPOINT, the same fault isolation the
            # garden classification drain uses: a template that cannot
            # be materialised (an appointment that would overlap, a
            # broken taxonomy) must not roll back the occurrences this
            # sweep already spawned.
            async with session.begin_nested():
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
                    due_date=(
                        rec.next_run if (not is_appointment and tmpl.due_date is not None) else None
                    ),
                    tag_ids=tag_ids,
                    assignee_ids=list(assignee_ids),
                )
                # Copy the template's reminder offsets onto the occurrence so it
                # fires the same reminders relative to its anchor.
                src_reminders = (
                    (
                        await session.execute(
                            select(TaskReminder).where(TaskReminder.task_id == rec.task_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                for sr in src_reminders:
                    session.add(
                        TaskReminder(
                            org_id=org_id,
                            task_id=new_task.id,
                            offset_minutes=sr.offset_minutes,
                            channels=sr.channels,
                        )
                    )
        except Exception:
            # Poison template: this occurrence is lost, but the schedule
            # still advances below so the drain makes progress instead
            # of re-attempting the same failing spawn every sweep.
            logger.exception("recurrence spawn failed (task_id=%s)", rec.task_id)
        else:
            rec.last_spawned_at = ref
            spawned += 1
        rec.next_run = _advance(rec.next_run, rec.freq, rec.interval)
        if rec.until is not None and rec.next_run > rec.until:
            rec.active = False
    await session.flush()
    return spawned


async def _user_reminder_ctx(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    cache: dict[uuid.UUID, tuple[dt.tzinfo, int, str]],
) -> tuple[dt.tzinfo, int, str]:
    """The recipient's ``(timezone, day_start_minute, language)``, memoised
    per sweep. The timezone renders labels in local time and detects the
    date-only sentinel; ``day_start_minute`` anchors a date-only task's
    reminders to the morning (``timewindow.day_start_anchor``); ``language``
    (en / it, default ``en``) localises the reminder title and body."""
    cached = cache.get(user_id)
    if cached is not None:
        return cached
    row = (
        await session.execute(
            select(User.timezone, User.day_start_minute, User.language).where(User.id == user_id)
        )
    ).one_or_none()
    ctx: tuple[dt.tzinfo, int, str]
    if row is None:
        ctx = (dt.UTC, DEFAULT_DAY_START_MINUTE, DEFAULT_LOCALE)
    else:
        ctx = (
            resolve_tz(row.timezone),
            row.day_start_minute or DEFAULT_DAY_START_MINUTE,
            row.language or DEFAULT_LOCALE,
        )
    cache[user_id] = ctx
    return ctx


def _humanize_offset(minutes: int, locale: str) -> str:
    """A reminder offset in minutes as a human string in ``locale`` (en /
    it): exact day / hour multiples read as "1 day" / "3 hours" ("1
    giorno" / "3 ore"); a mixed sub-day value as "2 hours 10 min"; a
    sub-hour value as "30 min". So the reminder body shows "(2 days
    before)" / "(2 giorni prima)" instead of "(2880 min before)"."""
    if minutes % 1440 == 0:
        d = minutes // 1440
        return render(
            MessageCode.DURATION_DAY if d == 1 else MessageCode.DURATION_DAYS, locale, n=d
        )
    if minutes < 60:
        return render(MessageCode.DURATION_MIN, locale, n=minutes)
    h, m = divmod(minutes, 60)
    hours = render(MessageCode.DURATION_HOUR if h == 1 else MessageCode.DURATION_HOURS, locale, n=h)
    if m == 0:
        return hours
    return f"{hours} {render(MessageCode.DURATION_MIN, locale, n=m)}"


# A reminder whose fire moment is more than this in the past is dropped
# rather than dispatched. It still covers a task added a little after its
# fire moment, but not a long-overdue task, nor a fire_at that re-surfaces
# with a fresh dedupe key (a release that changes the date-only anchor
# would otherwise re-send every past reminder at once -- the v2.0.93
# symptom). The task still shows overdue in the UI; this only governs the
# pre-due nudge.
STALE_GRACE = dt.timedelta(days=1)


async def scan_reminders(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    within_days: int = 1,
    now: dt.datetime | None = None,
) -> int:
    """Enqueue (idempotent) reminders for a task's recipients -- its owner,
    its collaborators, and its assignee when that assignee is a human
    identity -- that have an enabled channel. An AI-assistant assignee is
    never notified (it has no inbox; owner and collaborators are always real
    users by FK). Each task fires its configured ``task_reminders`` N
    minutes before a firing reference:

      * appointment tasks (``start_at`` set) fire relative to ``start_at``
        with minute precision;
      * date-only tasks (``due_date`` stored at end-of-day) anchor their
        reminders to the START of the due day -- ``day_start_minute``
        after local midnight in the recipient's timezone (configurable,
        default 0) -- NOT to the 23:59:59 end-of-day expiry sentinel. So
        "due today, no time" fires in the morning instead of at 23:59,
        which the user perceives as a day late. (The expiry/overdue
        boundary is unchanged: it is still end-of-day.)

    A deadline without explicit reminders gets one implicit reminder at
    the anchor. Sub-day offsets (``0 < off < 1440``) only have a defined
    firing minute when the task has a ``start_at``; on date-only tasks
    they are promoted to ``0`` (at the anchor) so they fire at a defined
    moment rather than silently bucketing to one day before the due date
    (the pre-v2.0.27 behaviour).

    The label, the date-only detection and the day-start anchor are all
    computed in the RECIPIENT's timezone (``users.timezone``, UTC when
    unset): a date-only ``due_date`` is end-of-day in the owner's zone,
    which round-trips to 23:59:59 there. The firing moment (``fire_at``)
    is an absolute instant and is timezone-independent."""
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
    ctx_cache: dict[uuid.UUID, tuple[dt.tzinfo, int, str]] = {}
    # Users with at least one browser push subscription: a webpush pref has
    # no per-pref target (the targets are the subscriptions), so it counts
    # as a usable channel only when the user has subscribed a device.
    users_with_push = set((await session.execute(select(PushSubscription.user_id))).scalars().all())
    # Members who can still act on what they are reminded about, computed
    # ONCE here rather than per recipient: the loop below is already
    # nested and does an N+1 through ``list_prefs``.
    #
    # A reminder is a nudge to do something. A deactivated user cannot log
    # in to do it, and an ex-member has no access to the workspace the task
    # lives in, so the mail is undeliverable in the sense that matters.
    # Scoped through ``memberships`` and not through a bare
    # ``users.is_active`` scan on purpose -- ``users`` carries no RLS
    # policy, so the unscoped form is a platform-wide read.
    live_members = set(
        (
            await session.execute(
                select(Membership.user_id)
                .join(User, User.id == Membership.user_id)
                .where(Membership.org_id == org_id, User.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    # Deep-link base for the task reference appended to every reminder body
    # (telegram/email render it clickable; the service worker opens it on a
    # webpush notification click). The SPA routes a single task at /tasks/<id>.
    base_url = get_settings().frontend_base_url.rstrip("/")
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
        reminder_rows = list(
            (await session.execute(select(TaskReminder).where(TaskReminder.task_id == t.id)))
            .scalars()
            .all()
        )
        # (offset_minutes, channels) per reminder; no explicit reminders ->
        # one implicit "at due" reminder on the recipient's default channels.
        reminder_specs: list[tuple[int, list[str] | None]] = [
            (r.offset_minutes, r.channels) for r in reminder_rows
        ] or [(0, None)]
        recipients = set(
            (
                await session.execute(
                    select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == t.id)
                )
            )
            .scalars()
            .all()
        )
        # Recipients = owner + collaborators + the assignee when it is a
        # human. owner_id and collaborator user_ids are always real users
        # (FK to ``users``), so no bot can appear there. The only recipient
        # that can be a bot is the primary ``assignee_id`` (an identity that
        # may be an AI assistant): resolve it and add it only when its kind
        # is ``user`` -- an assistant assignee is never notified (no inbox).
        recipients.add(t.owner_id)
        if t.assignee_id is not None:
            ident = (
                await session.execute(
                    select(Identity.kind, Identity.user_id).where(Identity.id == t.assignee_id)
                )
            ).one_or_none()
            if ident is not None and ident.kind == IdentityKind.user and ident.user_id is not None:
                recipients.add(ident.user_id)
        # ``recipients & live_members``: the owner, the collaborators and a
        # human assignee are all users, and any of the three can have been
        # deactivated or removed since the task was written. Whoever is left
        # gets the reminder; when nobody is, the task's deadline stops being
        # watched, which is a real gap and the reason the pickers refuse to
        # create this state in the first place.
        for uid in sorted(recipients & live_members, key=str):
            prefs = [
                p
                for p in await list_prefs(session, org_id=org_id, user_id=uid)
                if p.enabled
                and (
                    uid in users_with_push
                    if p.channel == NotificationChannelKind.webpush
                    else bool(p.target)
                )
            ]
            if not prefs:
                continue
            tz, day_start_minute, language = await _user_reminder_ctx(
                session, user_id=uid, cache=ctx_cache
            )
            # Reminder title in the recipient's language (same for every
            # offset on this task); the body adds the due moment + offset.
            rtitle = render(MessageCode.REMINDER_TITLE, language, title=t.title)
            local = reference.astimezone(tz)
            # date-only ("no time set") detection in the RECIPIENT's own
            # timezone: a date-only due is end-of-day in the owner's zone,
            # which round-trips to 23:59:59 there. Appointment tasks always
            # carry a real time.
            date_only = (
                is_due_date and local.hour == 23 and local.minute == 59 and local.second == 59
            )
            when_label = (
                local.date().isoformat() if date_only else local.strftime("%Y-%m-%d %H:%M %Z")
            )
            # Date-only tasks anchor to the START of the due day (day_start
            # after local midnight), NOT the 23:59:59 expiry sentinel, so a
            # "due today" reminder fires in the morning. Timed/appointment
            # tasks fire relative to their real instant.
            anchor = (
                day_start_anchor(local.date(), tz, day_start_minute) if date_only else reference
            )
            for raw_off, rchannels in reminder_specs:
                # Sub-day offsets on a date-only task have no defined
                # firing minute -> fire at the anchor ("at due").
                off = 0 if date_only and 0 < raw_off < 1440 else raw_off
                fire_at = anchor - dt.timedelta(minutes=off)
                if fire_at > horizon:
                    continue
                # Don't re-send a reminder whose moment is long past: an
                # overdue task, or a fire_at re-surfaced under a new dedupe
                # key, must not fire a stale pre-due nudge weeks late.
                if fire_at < ref - STALE_GRACE:
                    continue
                # Body in the recipient's language: the title is already the
                # notification title, so don't repeat it. Show the due moment
                # (date for date-only, date+time for appointments) + the
                # deep-link. Offset 0 omits the lead time; an early reminder
                # states it in human units (h/d), not raw minutes.
                detail = (
                    render(MessageCode.REMINDER_DUE, language, when=when_label)
                    if off == 0
                    else render(
                        MessageCode.REMINDER_DUE_BEFORE,
                        language,
                        when=when_label,
                        offset=_humanize_offset(off, language),
                    )
                )
                reminder_body = f"{detail}\n{base_url}/tasks/{t.id}"
                # Per-reminder channel selection: NULL channels = the
                # recipient's default (all usable prefs); a set list restricts
                # this reminder to those channels (intersected with usable).
                eff_prefs = (
                    prefs
                    if not rchannels
                    else [p for p in prefs if p.channel.value in set(rchannels)]
                )
                for p in eff_prefs:
                    await enqueue(
                        session,
                        org_id=org_id,
                        actor_id=actor_id,
                        user_id=uid,
                        channel=p.channel,
                        kind="reminder",
                        title=rtitle,
                        body=reminder_body,
                        dedupe_key=(
                            f"reminder:{t.id}:{uid}:{p.channel.value}:{fire_at.isoformat()}"
                        ),
                        fire_at=fire_at,
                        # Gate this reminder on the task's live state at
                        # dispatch: a reminder held until ``fire_at`` must be
                        # dropped if the task reaches a terminal state (or is
                        # archived / deleted) before it fires.
                        task_id=t.id,
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


def _normalize_channels(channels: list[str] | None) -> list[str] | None:
    """Keep only valid channel values (dedup, order-preserving). Empty / None
    -> None, meaning "the recipient's default" (all their enabled channels)."""
    if not channels:
        return None
    valid = {c.value for c in NotificationChannelKind}
    out: list[str] = []
    for c in channels:
        if c in valid and c not in out:
            out.append(c)
    return out or None


async def add_reminder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    offset_minutes: int,
    channels: list[str] | None = None,
) -> TaskReminder:
    await require_role(session, org_id, actor_id, Role.member)
    await tasks_svc.get_task(session, org_id=org_id, task_id=task_id)
    eff_channels = _normalize_channels(channels)
    existing = (
        await session.execute(
            select(TaskReminder).where(
                TaskReminder.task_id == task_id,
                TaskReminder.offset_minutes == offset_minutes,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Re-adding the same offset updates its channel selection.
        if existing.channels != eff_channels:
            existing.channels = eff_channels
            await session.flush()
        return existing
    r = TaskReminder(
        org_id=org_id,
        task_id=task_id,
        offset_minutes=max(0, offset_minutes),
        channels=eff_channels,
    )
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
