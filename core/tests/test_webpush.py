"""Web Push channel (#D): subscription CRUD, dispatch fan-out to all of a
user's devices with pruning of gone endpoints, and scan including the
webpush channel only when the user has at least one subscription.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.notification import NotificationChannelKind
from mycelium_core.services import notifications as nf
from mycelium_core.services import push_subscriptions as push_svc
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup
from mycelium_core.services.notifications_webpush import WebPushGone


class FakeWebPushSender:
    """Records webpush sends by endpoint; raises WebPushGone for any
    endpoint in ``gone`` (simulating a 404/410 from the push service)."""

    def __init__(self, gone: set[str] | None = None) -> None:
        self.sent: list[str] = []
        self.gone = gone or set()

    async def send(
        self, *, channel: NotificationChannelKind, target: str, title: str, body: str
    ) -> None:
        assert channel is NotificationChannelKind.webpush
        endpoint = json.loads(target)["endpoint"]
        if endpoint in self.gone:
            raise WebPushGone("410 Gone")
        self.sent.append(endpoint)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="WP")
    return r.org_id, r.user_id


async def test_subscription_upsert_idempotent_and_delete() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        await push_svc.upsert_subscription(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            endpoint="https://p/ep1",
            p256dh="k1",
            auth="a1",
        )
        # same endpoint -> refresh keys, not a second row
        await push_svc.upsert_subscription(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            endpoint="https://p/ep1",
            p256dh="k2",
            auth="a2",
        )
        subs = await push_svc.list_subscriptions(s, org_id=org, user_id=user)
        assert len(subs) == 1
        assert subs[0].p256dh == "k2"
        assert await push_svc.delete_subscription(
            s, org_id=org, actor_id=user, user_id=user, endpoint="https://p/ep1"
        )
        assert len(await push_svc.list_subscriptions(s, org_id=org, user_id=user)) == 0


async def test_webpush_dispatch_fans_out_and_prunes_gone() -> None:
    org, user = await _org()
    fake = FakeWebPushSender(gone={"https://p/dead"})
    async with tenant_session(str(org), str(user)) as s:
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.webpush,
            enabled=True,
            target="",
        )
        for ep in ("https://p/live1", "https://p/live2", "https://p/dead"):
            await push_svc.upsert_subscription(
                s, org_id=org, actor_id=user, user_id=user, endpoint=ep, p256dh="k", auth="a"
            )
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.webpush,
            kind="reminder",
            title="T",
            body="B",
        )
        res = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=fake)
        # one notification, delivered to >=1 live device -> counts as sent
        assert res.sent == 1
        assert res.failed == 0
        assert set(fake.sent) == {"https://p/live1", "https://p/live2"}
        # the dead endpoint was pruned
        remaining = {
            x.endpoint for x in await push_svc.list_subscriptions(s, org_id=org, user_id=user)
        }
        assert remaining == {"https://p/live1", "https://p/live2"}


async def test_webpush_dispatch_fails_without_subscriptions() -> None:
    org, user = await _org()
    fake = FakeWebPushSender()
    async with tenant_session(str(org), str(user)) as s:
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.webpush,
            enabled=True,
            target="",
        )
        await nf.enqueue(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.webpush,
            kind="reminder",
            title="T",
            body="B",
        )
        res = await nf.dispatch_pending(s, org_id=org, actor_id=user, sender=fake)
        assert res.sent == 0
        assert res.failed == 1
        assert fake.sent == []


async def test_scan_includes_webpush_only_with_subscription() -> None:
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        # webpush enabled but no device yet; disable the default email pref so
        # only webpush is in play.
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.webpush,
            enabled=True,
            target="",
        )
        await nf.set_pref(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            channel=NotificationChannelKind.email,
            enabled=False,
            target="",
        )
        start = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=2)
        await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="dated", start_at=start, duration_minutes=30
        )
        assert await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1) == 0
        # subscribe a device -> webpush becomes a usable channel
        await push_svc.upsert_subscription(
            s,
            org_id=org,
            actor_id=user,
            user_id=user,
            endpoint="https://p/ep",
            p256dh="k",
            auth="a",
        )
        assert await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1) >= 1
