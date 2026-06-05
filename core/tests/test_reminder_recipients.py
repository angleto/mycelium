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

from sqlalchemy import select

from flow_core.db import admin_session, tenant_session
from flow_core.models.identity import Identity
from flow_core.services import ai_assistants as ai_svc
from flow_core.services import notifications as nf
from flow_core.services import tasks as tasks_svc
from flow_core.services.auth import signup


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
