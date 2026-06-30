"""Reminder notification text: the body carries the task deep-link, does not
repeat the title (it's already the notification title), and omits the
redundant "(at due)" suffix for offset-0 reminders. A re-enqueue refreshes a
still-pending row so queued reminders pick up an improved message.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.notification import NotificationChannelKind
from mycelium_core.services import notifications as nf
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="RT")
    return r.org_id, r.user_id


async def test_reminder_body_has_link_no_dup_title_no_at_due() -> None:
    org, user = await _org()  # signup seeds an enabled email pref
    async with tenant_session(str(org), str(user)) as s:
        due = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=3)
        task = await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="solo dated", due_date=due
        )
        await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
        rem = next(x for x in notes if x.kind == "reminder")
    assert f"/tasks/{task.id}" in rem.body  # deep-link present
    assert "(at due)" not in rem.body  # no redundant suffix on offset 0
    assert "solo dated" not in rem.body  # title not repeated in the body
    assert rem.title == "Task due: solo dated"  # the title carries the name


async def test_enqueue_refreshes_pending_body() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        n1 = await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="reminder",
            title="T1",
            body="old body",
            dedupe_key="dk:refresh:1",
            fire_at=None,
        )
        n2 = await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            kind="reminder",
            title="T2",
            body="new body",
            dedupe_key="dk:refresh:1",
            fire_at=None,
        )
    assert n1.id == n2.id  # same row (dedup on dedupe_key)
    assert n2.body == "new body"  # not-yet-sent row refreshed in place
    assert n2.title == "T2"
