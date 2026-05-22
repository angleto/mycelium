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

import pytest

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.models.dependency import DependencyType
from flow_core.models.notification import NotificationChannelKind, RecurrenceFreq
from flow_core.notification_channel import set_sender_override
from flow_core.services import dependencies as deps
from flow_core.services import notifications as nf
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup


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
        due = (dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=12)).date()
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
            await nf.delete_notification(
                s, org_id=org, actor_id=user, notification_id=uuid.uuid4()
            )
        # A notification addressed to someone else is not deletable by the
        # actor (scoped to user_id == actor_id).
        with pytest.raises(DomainError):
            await nf.delete_notification(
                s, org_id=org, actor_id=uuid.uuid4(), notification_id=n.id
            )
        await nf.delete_notification(s, org_id=org, actor_id=user, notification_id=n.id)
        assert await nf.list_notifications(s, org_id=org, user_id=user) == []
