"""scan_reminders recipients (#E).

The task owner is reminded about their own dated task even with no
collaborator row, and an AI-assistant *assignee* is never reminded (it has
no inbox). owner_id and collaborator user_ids are always real users (FK to
``users``), so the only recipient that can be a bot is the primary
``assignee_id`` (an identity that may be an AI assistant) -- it is added
only when its kind is ``user``.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.identity import Identity
from mycelium_core.models.notification import Notification, NotificationChannelKind
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.models.user import User
from mycelium_core.services import ai_assistants as ai_svc
from mycelium_core.services import memberships as mem_svc
from mycelium_core.services import notifications as nf
from mycelium_core.services import tasks as tasks_svc
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _org() -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        r = await signup(s, email=_email(), password="pw-strong-123", org_name="REC")
    return r.org_id, r.user_id


async def test_owner_notified_without_collaborators() -> None:
    """A dated task owned by the user, with no collaborators and no explicit
    assignee, still reminds the owner (who carries the default email pref
    seeded at provisioning). Before #E this enqueued nothing."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        start = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=2)
        await tasks_svc.create_task(
            s, org_id=org, actor_id=user, title="solo dated", start_at=start, duration_minutes=30
        )
        n = await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        reminders = [
            x
            for x in await nf.list_notifications(s, org_id=org, user_id=user)
            if x.kind == "reminder"
        ]
    assert n >= 1
    assert reminders
    assert all(note.user_id == user for note in reminders)


async def test_ai_assistant_assignee_not_notified() -> None:
    """A bot assignee receives no reminder; only the human owner does."""
    org, user = await _org()
    async with tenant_session(str(org), str(user)) as s:
        res = await ai_svc.create_assistant(s, org_id=org, actor_id=user, label="Bot")
        bot_identity = (
            await s.execute(select(Identity).where(Identity.ai_assistant_id == res.assistant.id))
        ).scalar_one()
        start = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=2)
        await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="bot assigned",
            start_at=start,
            duration_minutes=30,
            assignee_id=bot_identity.id,
        )
        n = await nf.scan_reminders(s, org_id=org, actor_id=user, within_days=1)
        reminders = [
            x
            for x in await nf.list_notifications(s, org_id=org, user_id=user)
            if x.kind == "reminder"
        ]
    # Exactly the human owner is reminded; the bot assignee adds no recipient.
    assert n == 1
    assert all(note.user_id == user for note in reminders)


async def test_a_collaborator_who_can_no_longer_act_is_not_reminded() -> None:
    """A reminder is a nudge to go do something. A deactivated user cannot
    log in to do it and an ex-member has no access to the workspace the
    task lives in, so both stop being recipients while the owner keeps
    being one. Their collaborator ROW is untouched -- this is a delivery
    rule, not a rewrite of who is on the task."""
    owner_email, dead_email, gone_email = _email(), _email(), _email()
    async with admin_session() as s:
        a = await signup(s, email=owner_email, password="pw-strong-123", org_name="REC")
        dead = await signup(s, email=dead_email, password="pw-strong-123", org_name="D")
        gone = await signup(s, email=gone_email, password="pw-strong-123", org_name="G")
    org, owner = a.org_id, a.user_id

    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=dead_email, role="member")
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=gone_email, role="member")
        start = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=2)
        task = await tasks_svc.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="shared dated",
            start_at=start,
            duration_minutes=30,
        )
        for uid in (dead.user_id, gone.user_id):
            await tasks_svc.assign(s, org_id=org, actor_id=owner, task_id=task.id, user_id=uid)
            # A usable channel IN THIS ORG. Without it they would receive
            # nothing whatever the filter does, and the assertion below
            # would pass with the fix removed.
            await nf.set_pref(
                s,
                org_id=org,
                actor_id=owner,
                user_id=uid,
                channel=NotificationChannelKind.email,
                enabled=True,
                target=f"{uid}@example.test",
            )
        task_id = task.id

    async with admin_session() as s:
        await s.execute(update(User).where(User.id == dead.user_id).values(is_active=False))
    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.remove_member(s, org_id=org, actor_id=owner, target_user_id=gone.user_id)

    async with tenant_session(str(org), str(owner)) as s:
        await nf.scan_reminders(s, org_id=org, actor_id=owner, within_days=1)
        # Queried off the table, NOT through list_notifications: that one
        # filters by user_id, so asking it about the owner could only ever
        # answer "the owner" and would pass with the filter removed.
        reminded = {
            n.user_id
            for n in (
                await s.execute(
                    select(Notification).where(
                        Notification.task_id == task_id, Notification.kind == "reminder"
                    )
                )
            )
            .scalars()
            .all()
        }
        assert reminded == {owner}, f"only the owner can still act on it, got {reminded}"
        # The rows themselves are untouched: filtering is about delivery.
        collabs = (
            (
                await s.execute(
                    select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
        assert set(collabs) == {dead.user_id, gone.user_id}
