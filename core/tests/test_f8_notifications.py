"""F8 notifications / recurrence / reminders (DB-backed), FR-12.

Idempotent enqueue, fault-isolated dispatch honouring per-user channel
prefs, recurrence/dependency mutual exclusion, deterministic spawn of
independent task rows, idempotent reminder scan, cross-org isolation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.models.dependency import DependencyType
from mycelium_core.models.notification import (
    NotificationChannelKind,
    NotificationStatus,
    RecurrenceFreq,
)
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.notification_channel import set_sender_override
from mycelium_core.services import dependencies as deps
from mycelium_core.services import notifications as nf
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services import users as users_svc
from mycelium_core.services.auth import signup
from mycelium_core.services.mailer import OutboundEmail, set_mailer
from mycelium_core.services.notification_sender import build_notification_sender
from mycelium_core.telegram_client import set_telegram_api_override


class FakeSender:
    def __init__(self, fail_targets: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail_targets or set()

    async def send(
        self, *, channel: NotificationChannelKind, target: str, title: str, body: str
    ) -> None:
        if target in self.fail:
            raise RuntimeError("channel down")
        self.sent.append((target, title))


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="NTF")
    return r.org_id, r.user_id


async def test_enqueue_idempotent_and_dispatch_respects_prefs() -> None:
    org, user = await _org()
    snd = FakeSender()
    async with tenant_session(str(org), str(user)) as s:
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            enabled=True,
            target="me@example.test",
        )
        a = await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="test",
            title="Hi",
            body="b",
            dedupe_key="k1",
        )
        b = await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="test",
            title="Hi",
            body="b",
            dedupe_key="k1",
        )
        assert a.id == b.id  # idempotent
        r = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=snd)
    assert (r.sent, r.failed) == (1, 0)
    assert snd.sent == [("me@example.test", "Hi")]


async def test_dispatch_fault_isolation_and_missing_pref() -> None:
    org, user = await _org()
    snd = FakeSender(fail_targets={"bad@example.test"})
    async with tenant_session(str(org), str(user)) as s:
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            enabled=True,
            target="bad@example.test",
        )
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="t",
            title="A",
            body="b",
            dedupe_key="a",
        )
        # telegram has no pref -> that one fails, email failure isolated.
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.telegram,
            kind="t",
            title="B",
            body="b",
            dedupe_key="b",
        )
        r = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=snd)
    assert r.failed == 2 and r.sent == 0  # one channel error, one no-pref


async def _terminal_state_id(s) -> uuid.UUID:
    return (
        await s.execute(
            select(WorkflowState.id).where(WorkflowState.is_terminal.is_(True)).limit(1)
        )
    ).scalar_one()


async def test_dispatch_suppresses_notification_for_terminal_task() -> None:
    """A reminder enqueued ahead of time must NOT fire once its task reaches
    a terminal state. ``scan_reminders`` only filters terminal tasks at
    ENQUEUE time; dispatch re-validates eligibility at SEND time and drops
    the queued row (neither sent nor failed) so it never fires late."""
    org, user = await _org()
    snd = FakeSender()
    async with tenant_session(str(org), str(user)) as s:
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            enabled=True,
            target="me@example.test",
        )
        t = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="close-me", assignee_ids=[user]
        )
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="reminder",
            title="R",
            body="b",
            dedupe_key="rem-1",
            task_id=t.id,
        )
        # Task closes AFTER the reminder is already queued.
        t.state_id = await _terminal_state_id(s)
        await s.flush()
        r = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=snd)
        remaining = await nf.list_notifications(s, org_id=org, user_id=user)
    assert (r.sent, r.failed, r.suppressed) == (0, 0, 1)
    assert snd.sent == []  # never delivered
    assert list(remaining) == []  # queued row dropped, not left pending forever


async def test_dispatch_suppresses_for_archived_or_deleted_task() -> None:
    """The dispatch gate also drops a notification whose task is archived or
    soft-deleted -- not only terminal-state ones -- so the single
    send-time check covers every way a task becomes ineligible."""
    for mutate in ("archive", "delete"):
        org, user = await _org()
        snd = FakeSender()
        async with tenant_session(str(org), str(user)) as s:
            await nf.set_pref(
                s,
                org_id=org,
                actor_id=user,
                user_id=user,
                channel=NotificationChannelKind.email,
                enabled=True,
                target="me@example.test",
            )
            t = await tasks_svc.create_task(
                s, org_id=org, actor_id=user, title="x", assignee_ids=[user]
            )
            await nf.enqueue(
                s,
                org_id=org,
                actor_id=user,
                user_id=user,
                channel=NotificationChannelKind.email,
                kind="reminder",
                title="R",
                body="b",
                dedupe_key="rem",
                task_id=t.id,
            )
            if mutate == "archive":
                t.is_archived = True
            else:
                t.deleted_at = dt.datetime.now(tz=dt.UTC)
            await s.flush()
            r = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=snd)
        assert (r.sent, r.failed, r.suppressed) == (0, 0, 1), mutate
        assert snd.sent == [], mutate


async def test_dispatch_gate_does_not_over_suppress() -> None:
    """The eligibility gate must leave alone a notification whose task is
    still active, AND one with no task linkage (coordination offers/handoffs
    carry ``task_id=None`` and fire immediately by design)."""
    org, user = await _org()
    snd = FakeSender()
    async with tenant_session(str(org), str(user)) as s:
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            enabled=True,
            target="me@example.test",
        )
        t = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="live", assignee_ids=[user]
        )
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="reminder",
            title="R",
            body="b",
            dedupe_key="rem",
            task_id=t.id,  # task still active
        )
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="task_offer",
            title="O",
            body="b",
            dedupe_key="off",
            task_id=None,  # not task-gated
        )
        r = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=snd)
    assert (r.sent, r.failed, r.suppressed) == (2, 0, 0)
    assert {title for (_, title) in snd.sent} == {"R", "O"}


async def test_recurrence_excludes_dependencies_and_spawns() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        a = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="A")
        b = await tasks_svc.create_task(s, org_id=org, actor_id=user, title="B")
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=a.id,
            successor_id=b.id,
            type=DependencyType.FS,
        )
        with pytest.raises(DomainError):  # recurrence + deps mutually exclusive
            await nf.create_recurrence(
                s,
                org_id=org,
                actor_id=user,
                task_id=a.id,
                freq=RecurrenceFreq.daily,
                next_run=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            )
        tmpl = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Weekly report",
            estimate_effort_h=Decimal(2),
        )
        await nf.create_recurrence(
            s,
            org_id=org,
            actor_id=user,
            task_id=tmpl.id,
            freq=RecurrenceFreq.weekly,
            next_run=dt.datetime(2026, 1, 5, tzinfo=dt.UTC),
        )
        ref = dt.datetime(2026, 1, 6, tzinfo=dt.UTC)
        n1 = await nf.spawn_due(s, org_id=org, actor_id=user, now=ref)
        n2 = await nf.spawn_due(s, org_id=org, actor_id=user, now=ref)
        all_tasks = await tasks_svc.list_tasks(s, org_id=org)
    assert n1 == 1 and n2 == 0  # advanced; not due again at same ref
    spawned = [t for t in all_tasks if t.title == "Weekly report" and t.id != tmpl.id]
    assert len(spawned) == 1  # an independent new task row


async def test_reminder_scan_minute_precise_on_appointment() -> None:
    """Sub-day offsets on appointment tasks (``start_at`` set) must
    fire at minute precision off ``start_at``, not collapse to one day
    before (the pre-v2.0.27 day-bucketed math)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            enabled=True,
            target="me@example.test",
        )
        start = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=2)
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="meeting",
            start_at=start,
            duration_minutes=30,
            assignee_ids=[user],
        )
        await nf.add_reminder(s, org_id=org, actor_id=user, task_id=task.id, offset_minutes=30)
        # Reference is "now"; the 30-min offset fires at start-30min,
        # i.e. 90min from now -- still within the 1-day look-ahead.
        n = await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
    assert n == 1
    reminder_notes = [x for x in notes if x.kind == "reminder"]
    assert len(reminder_notes) == 1
    assert "30 min before" in reminder_notes[0].body


async def test_reminder_scan_promotes_subday_on_date_only() -> None:
    """A 60-minute offset on a date-only task (``due_date`` set,
    ``start_at`` unset) has no defined firing minute. It must be
    promoted to 0 (alla scadenza) so it fires at midnight UTC of the
    due date rather than silently bucketing one day earlier."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            enabled=True,
            target="me@example.test",
        )
        # Migration 0005: due_date is timestamptz; "no time specified"
        # is end-of-day UTC (the SPA's convention).
        due_day = (dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=6)).date()
        due = dt.datetime.combine(due_day, dt.time(23, 59, 59), tzinfo=dt.UTC)
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="due today",
            due_date=due,
            assignee_ids=[user],
        )
        await nf.add_reminder(s, org_id=org, actor_id=user, task_id=task.id, offset_minutes=60)
        n = await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
    assert n == 1
    reminder_notes = [x for x in notes if x.kind == "reminder"]
    assert len(reminder_notes) == 1
    # Promoted to 0 (date-only): plain "Due <date>", no "min before" and no
    # redundant "(at due)" suffix.
    assert "min before" not in reminder_notes[0].body
    assert "(at due)" not in reminder_notes[0].body


async def test_reminder_scan_is_idempotent() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            enabled=True,
            target="me@example.test",
        )
        due_day = (dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=12)).date()
        due = dt.datetime.combine(due_day, dt.time(23, 59, 59), tzinfo=dt.UTC)
        await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="due soon",
            due_date=due,
            assignee_ids=[user],
        )
        n1 = await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        n2 = await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
    assert n1 == 1 and n2 == 1  # enqueue called, but dedupe keeps one row
    assert len([x for x in notes if x.kind == "reminder"]) == 1


@pytest.fixture
def _sender() -> Iterator[FakeSender]:
    snd = FakeSender()
    set_sender_override(lambda: snd)
    try:
        yield snd
    finally:
        set_sender_override(None)


async def test_notifications_org_isolated(_sender: FakeSender) -> None:
    a_org, a_user = await _org()
    b_org, b_user = await _org()
    async with tenant_session(str(a_org), str(a_user)) as s:
        await nf.enqueue(
            s,
            org_id=a_org,
            actor_id=a_user,
            user_id=a_user,
            channel=NotificationChannelKind.email,
            kind="t",
            title="secret",
            body="b",
            dedupe_key="x",
        )
    async with tenant_session(str(b_org), str(b_user)) as s:
        assert await nf.list_notifications(s, org_id=b_org) == []


async def test_delete_notification_dismisses_own_only() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        n = await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="task_offer",
            title="Offered",
            body="b",
            dedupe_key="del-1",
        )
        # Unknown id -> domain error (not silently ignored).
        with pytest.raises(DomainError):
            await nf.delete_notification(s, org_id=org, actor_id=user, notification_id=uuid.uuid4())
        # A notification addressed to someone else is not deletable by the
        # actor (scoped to user_id == actor_id).
        with pytest.raises(DomainError):
            await nf.delete_notification(s, org_id=org, actor_id=uuid.uuid4(), notification_id=n.id)
        await nf.delete_notification(s, org_id=org, actor_id=user, notification_id=n.id)
        assert await nf.list_notifications(s, org_id=org, user_id=user) == []


async def _enable_email(s, org, user, target: str = "me@example.test") -> None:
    await nf.set_pref(
        s,
        org_id=org,
        actor_id=user,
        user_id=user,
        channel=NotificationChannelKind.email,
        enabled=True,
        target=target,
    )


async def test_dispatch_gates_on_fire_at() -> None:
    """A reminder enqueued ahead of its firing moment is HELD by
    dispatch_pending until ``fire_at`` arrives; ``fire_at`` in the past
    or NULL sends immediately."""
    org, user = await _org()
    snd = FakeSender()
    now = dt.datetime.now(tz=dt.UTC)
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email(s, org, user)
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="reminder",
            title="future",
            body="b",
            dedupe_key="future",
            fire_at=now + dt.timedelta(hours=3),
        )
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="reminder",
            title="due",
            body="b",
            dedupe_key="due",
            fire_at=now - dt.timedelta(minutes=1),
        )
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="offer",
            title="immediate",
            body="b",
            dedupe_key="imm",
            fire_at=None,
        )
        r = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=snd)
        pending = await nf.list_notifications(
            s, org_id=org, user_id=user, status=NotificationStatus.pending
        )
    assert (r.sent, r.failed) == (2, 0)
    assert {title for _t, title in snd.sent} == {"due", "immediate"}  # "future" held
    assert [n.title for n in pending] == ["future"]


async def test_scan_skips_reminder_beyond_horizon() -> None:
    """The negative case for the look-ahead window: an appointment 5 days
    out with a 30-min reminder is NOT enqueued under within_days=1 (its
    fire_at is beyond the horizon)."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email(s, org, user)
        start = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=5)
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="far meeting",
            start_at=start,
            duration_minutes=30,
            assignee_ids=[user],
        )
        await nf.add_reminder(s, org_id=org, actor_id=user, task_id=task.id, offset_minutes=30)
        n = await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
    assert n == 0
    assert [x for x in notes if x.kind == "reminder"] == []


async def test_failed_reminder_revived_for_retry() -> None:
    """A transient send failure marks the row failed; a later enqueue
    (same dedupe_key) revives it to pending so the next dispatch retries.
    A working sender then delivers it."""
    org, user = await _org()
    failing = FakeSender(fail_targets={"me@example.test"})
    ok = FakeSender()
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email(s, org, user)
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="reminder",
            title="T",
            body="b",
            dedupe_key="k",
            fire_at=None,
        )
        r1 = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=failing)
        assert (r1.sent, r1.failed) == (0, 1)
        revived = await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="reminder",
            title="T",
            body="b",
            dedupe_key="k",
            fire_at=None,
        )
        assert revived.status is NotificationStatus.pending and revived.attempts == 1
        r2 = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=ok)
        assert (r2.sent, r2.failed) == (1, 0)
        assert revived.status is NotificationStatus.sent


async def test_failed_reminder_retry_capped() -> None:
    """Retry of a permanently-failing target is bounded: after
    MAX_NOTIFICATION_ATTEMPTS failures the row is no longer revived."""
    org, user = await _org()
    failing = FakeSender(fail_targets={"me@example.test"})
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email(s, org, user)
        for _ in range(nf.MAX_NOTIFICATION_ATTEMPTS):
            await nf.enqueue(
                s,
                org_id=org,
                actor_id=user,
                user_id=user,
                channel=NotificationChannelKind.email,
                kind="reminder",
                title="T",
                body="b",
                dedupe_key="cap",
                fire_at=None,
            )
            await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=failing)
        capped = await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="reminder",
            title="T",
            body="b",
            dedupe_key="cap",
            fire_at=None,
        )
    assert capped.attempts == nf.MAX_NOTIFICATION_ATTEMPTS
    assert capped.status is NotificationStatus.failed  # cap reached: not revived


async def test_reminder_label_and_dateonly_in_user_timezone() -> None:
    """A date-only due (end-of-day in the user's own timezone) is detected
    as date-only and the offset promoted, even though the stored instant
    is not 23:59:59 in UTC. The label is rendered in the user's zone."""
    org, user = await _org()
    async with admin_session() as s:
        await users_svc.set_timezone(s, user_id=user, timezone="America/New_York")
    ny = ZoneInfo("America/New_York")
    due_day = (dt.datetime.now(tz=ny) + dt.timedelta(days=2)).date()
    due = dt.datetime.combine(due_day, dt.time(23, 59, 59), tzinfo=ny).astimezone(dt.UTC)
    # Sanity: the stored UTC instant is NOT 23:59:59 (so the old UTC-only
    # heuristic would have missed it).
    assert (due.hour, due.minute, due.second) != (23, 59, 59)
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email(s, org, user)
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="ny deadline",
            due_date=due,
            assignee_ids=[user],
        )
        await nf.add_reminder(s, org_id=org, actor_id=user, task_id=task.id, offset_minutes=60)
        n = await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=7)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
    assert n == 1
    reminder = next(x for x in notes if x.kind == "reminder")
    # Date-only: labelled with the local date (not a UTC time), no "(at due)".
    assert "(at due)" not in reminder.body
    assert due_day.isoformat() in reminder.body
    assert "UTC" not in reminder.body


async def test_dateonly_reminder_anchors_to_day_start() -> None:
    """A date-only deadline (stored end-of-day in the owner's tz) fires
    its reminder at the user's configured day-start time IN THEIR
    timezone -- not at 23:59:59 (the end-of-day expiry sentinel), which
    read as a day late. day_start_minute=360 -> 06:00 local."""
    org, user = await _org()
    rome = ZoneInfo("Europe/Rome")
    async with admin_session() as s:
        await users_svc.update_profile(
            s, user_id=user, timezone="Europe/Rome", day_start_minute=360
        )
    # A bare date is date-only: the service stores end-of-day in the
    # owner's (Rome) timezone.
    due_day = (dt.datetime.now(tz=rome) + dt.timedelta(days=2)).date()
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email(s, org, user)
        await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="report", due_date=due_day, assignee_ids=[user]
        )
        await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=7)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
    reminder = next(x for x in notes if x.kind == "reminder")
    assert reminder.fire_at is not None
    local = reminder.fire_at.astimezone(rome)
    assert (local.hour, local.minute, local.second) == (6, 0, 0)
    assert local.date() == due_day


async def test_reminder_text_localized_italian() -> None:
    """An ``it`` recipient gets the reminder title/body and the humanised
    lead time in Italian -- "(2 giorni prima)", not "(2880 min before)"."""
    org, user = await _org()
    rome = ZoneInfo("Europe/Rome")
    async with admin_session() as s:
        await users_svc.update_profile(s, user_id=user, language="it", timezone="Europe/Rome")
    # Date-only, due in 5 days, "2 days before" -> fires in 3 days (within
    # the look-ahead, not stale).
    due_day = (dt.datetime.now(tz=rome) + dt.timedelta(days=5)).date()
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email(s, org, user)
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="Camp", due_date=due_day, assignee_ids=[user]
        )
        await nf.add_reminder(s, org_id=org, actor_id=user, task_id=task.id, offset_minutes=2880)
        await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=7)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
    reminder = next(x for x in notes if x.kind == "reminder")
    assert reminder.title.startswith("Attività in scadenza:")
    assert "In scadenza" in reminder.body
    assert "2 giorni prima" in reminder.body
    assert "min before" not in reminder.body and "days before" not in reminder.body


async def test_stale_overdue_reminder_dropped() -> None:
    """A reminder whose fire moment is weeks in the past (an overdue task,
    or a re-surfaced fire_at) is not enqueued -- no stale nudge."""
    org, user = await _org()
    due = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=30)
    due_eod = dt.datetime.combine(due.date(), dt.time(23, 59, 59), tzinfo=dt.UTC)
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email(s, org, user)
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="old", due_date=due_eod, assignee_ids=[user]
        )
        await nf.add_reminder(s, org_id=org, actor_id=user, task_id=task.id, offset_minutes=1440)
        n = await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
    assert n == 0
    assert [x for x in notes if x.kind == "reminder"] == []


async def test_dateonly_reminder_default_day_start_is_local_midnight() -> None:
    """With no day-start configured, a date-only reminder anchors to local
    midnight (start of day), not the 23:59:59 end-of-day sentinel."""
    org, user = await _org()
    rome = ZoneInfo("Europe/Rome")
    async with admin_session() as s:
        await users_svc.set_timezone(s, user_id=user, timezone="Europe/Rome")
    due_day = (dt.datetime.now(tz=rome) + dt.timedelta(days=2)).date()
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email(s, org, user)
        await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="report", due_date=due_day, assignee_ids=[user]
        )
        await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=7)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
    reminder = next(x for x in notes if x.kind == "reminder")
    assert reminder.fire_at is not None
    local = reminder.fire_at.astimezone(rome)
    assert (local.hour, local.minute, local.second) == (0, 0, 0)
    assert local.date() == due_day


async def test_recurrence_spawn_copies_timing_and_reminders() -> None:
    """A spawned occurrence is anchored on the recurrence's next_run and
    inherits the template's reminder offsets (so recurring tasks actually
    fire reminders)."""
    org, user = await _org()
    next_run = dt.datetime(2026, 1, 5, 9, 0, tzinfo=dt.UTC)
    async with tenant_session(str(org), str(user)) as s:
        tmpl = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Weekly report",
            due_date=dt.datetime(2026, 1, 1, 23, 59, 59, tzinfo=dt.UTC),
            assignee_ids=[user],
        )
        await nf.add_reminder(s, org_id=org, actor_id=user, task_id=tmpl.id, offset_minutes=60)
        await nf.create_recurrence(
            s,
            org_id=org,
            actor_id=user,
            task_id=tmpl.id,
            freq=RecurrenceFreq.weekly,
            next_run=next_run,
        )
        n = await nf.spawn_due(
            s, org_id=org, actor_id=user, now=dt.datetime(2026, 1, 6, tzinfo=dt.UTC)
        )
        all_tasks = await tasks_svc.list_tasks(s, org_id=org)
        spawned = [t for t in all_tasks if t.title == "Weekly report" and t.id != tmpl.id]
        occ = spawned[0]
        rems = await nf.list_reminders(s, org_id=org, task_id=occ.id)
    assert n == 1 and len(spawned) == 1
    assert occ.due_date == next_run  # anchored on next_run, not the template's date
    assert [r.offset_minutes for r in rems] == [60]


async def test_build_notification_sender_routes_by_channel() -> None:
    """The wired sender delegates email to the system mailer and telegram
    to the Telegram API (the seam that was never installed in prod)."""
    emails: list[OutboundEmail] = []
    tg: list[tuple[int, str]] = []

    class _FakeMailer:
        async def send(self, message: OutboundEmail) -> None:
            emails.append(message)

    class _FakeTg:
        async def send_message(self, *, chat_id: int, text: str) -> None:
            tg.append((chat_id, text))

    set_mailer(_FakeMailer())  # reset to LogMailer by the conftest autouse fixture
    set_telegram_api_override(lambda: _FakeTg())  # type: ignore[arg-type]  # only send_message used
    try:
        sender = build_notification_sender()
        await sender.send(
            channel=NotificationChannelKind.email, target="x@y.test", title="S", body="B"
        )
        await sender.send(
            channel=NotificationChannelKind.telegram, target="123", title="S", body="B"
        )
    finally:
        set_telegram_api_override(None)
    assert len(emails) == 1 and emails[0].to == "x@y.test"
    assert tg == [(123, "S\n\nB")]
