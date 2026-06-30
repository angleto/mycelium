"""Per-reminder channel selection (#G).

A reminder with explicit ``channels`` fires only on those (intersected with
the recipient's usable prefs); NULL channels fall back to the user's default
(all their enabled channels). add_reminder normalizes the channel list.
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
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="RC")
    return r.org_id, r.user_id


async def _enable_email_and_telegram(s: object, org: uuid.UUID, user: uuid.UUID) -> None:
    for channel, target in (
        (NotificationChannelKind.email, "me@example.test"),
        (NotificationChannelKind.telegram, "123456"),
    ):
        await nf.set_pref(
            s,  # type: ignore[arg-type]
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=channel,
            enabled=True,
            target=target,
        )


async def _dated_task(s: object, org: uuid.UUID, user: uuid.UUID) -> uuid.UUID:
    start = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=2)
    t = await tasks_svc.create_task(
        s,  # type: ignore[arg-type]
        org_id=org,
        actor_id=user,
        title="dated",
        start_at=start,
        duration_minutes=30,
    )
    return t.id


async def test_add_reminder_normalizes_channels() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        task_id = await _dated_task(s, org, user)
        # invalid + duplicate dropped
        r = await nf.add_reminder(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            offset_minutes=30,
            channels=["email", "bogus", "email"],
        )
        assert r.channels == ["email"]
        # empty -> None (= default)
        r2 = await nf.add_reminder(
            s, org_id=org, actor_id=user, task_id=task_id, offset_minutes=60, channels=[]
        )
        assert r2.channels is None
        # re-adding the same offset updates the channels in place
        r3 = await nf.add_reminder(
            s, org_id=org, actor_id=user, task_id=task_id, offset_minutes=30, channels=["telegram"]
        )
        assert r3.id == r.id
        assert r3.channels == ["telegram"]


async def test_reminder_channels_restrict_dispatch() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email_and_telegram(s, org, user)
        task_id = await _dated_task(s, org, user)
        await nf.add_reminder(
            s, org_id=org, actor_id=user, task_id=task_id, offset_minutes=0, channels=["email"]
        )
        await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
        channels = {n.channel for n in notes if n.kind == "reminder"}
    # pinned to email -> telegram excluded even though it is enabled
    assert channels == {NotificationChannelKind.email}


async def test_reminder_null_channels_uses_all_enabled() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await _enable_email_and_telegram(s, org, user)
        task_id = await _dated_task(s, org, user)
        await nf.add_reminder(s, org_id=org, actor_id=user, task_id=task_id, offset_minutes=0)
        await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        notes = await nf.list_notifications(s, org_id=org, user_id=user)
        channels = {n.channel for n in notes if n.kind == "reminder"}
    # no channels set -> the user's default: every enabled channel
    assert channels == {NotificationChannelKind.email, NotificationChannelKind.telegram}
