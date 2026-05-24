"""Scheduler integration of appointment-task participants
(migration 0095/0096, ADR-0008 addendum).

The 0096 trigger mirrors the assignee into ``task_participants``, and
the participants service can add extra identities. The scheduler must
see ALL of them as busy when placing plain work-tasks: a participant's
appointment blocks their other tasks from overlapping the window, just
like the assignee's does.

Style mirrors api/tests/test_scheduler_p1.py.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from flow_core.db import admin_session, tenant_session
from flow_core.services import actors as actors_svc
from flow_core.services import identities as identities_svc
from flow_core.services import memberships as mem_svc
from flow_core.services import participants as p_svc
from flow_core.services import scheduler as sch
from flow_core.services import tasks
from flow_core.services.auth import signup

_RM = ZoneInfo("Europe/Rome")
# Monday 2026-08-03 09:00 Europe/Rome (summer = UTC+2).
_AS_OF = dt.datetime(2026, 8, 3, 7, 0, tzinfo=dt.UTC)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _mint(s, *, user, seed: str):
    await actors_svc.mint_user_handle(s, user_id=user, seed=seed)


async def test_participant_appointment_pushes_plain_work() -> None:
    """Owner schedules a 1h plain work-task for the collaborator. A
    1h appointment exists with the collaborator as participant
    overlapping the collab's 09:00-10:00 work slot. The scheduler must
    push the work past the appointment, not overlap it."""
    owner_email = _email()
    collab_email = _email()
    async with admin_session() as s:
        a = await signup(s, email=owner_email, password="pw-strong-123", org_name="P")
        b = await signup(s, email=collab_email, password="pw-strong-123", org_name="O")
    org, owner = a.org_id, a.user_id
    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=collab_email, role="member")
    async with tenant_session(str(org), str(owner)) as s:
        await _mint(s, user=owner, seed=owner_email)
        await _mint(s, user=b.user_id, seed=collab_email)
        owner_ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=owner)
        collab_ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=b.user_id)
        # Plain 1h work for the collaborator.
        work = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Collab work",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal(1),
            assignee_id=collab_ident.id,
            assignee_ids=[b.user_id],  # populates task_collaborators so the
            # scheduler picks the human-assignee fallback when needed.
        )
        # Owner's 09:30-10:30 meeting with the collab as participant.
        meet = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Meeting",
            assignee_id=owner_ident.id,
            start_at=dt.datetime(2026, 8, 3, 7, 30, tzinfo=dt.UTC),  # 09:30 RM
            duration_minutes=60,
        )
        await p_svc.add_participant(
            s, org_id=org, actor_id=owner, task_id=meet.id, identity_id=collab_ident.id
        )
        # Recompute.
        await sch.recompute(s, org_id=org, actor_id=owner, as_of=_AS_OF)
        by_id = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}
        # The appointment must occupy 09:30-10:30 Rome exactly.
        appt = by_id[meet.id]
        assert appt.scheduled_start == dt.datetime(2026, 8, 3, 7, 30, tzinfo=dt.UTC)
        assert appt.scheduled_end == dt.datetime(2026, 8, 3, 8, 30, tzinfo=dt.UTC)
        # The collab's plain work cannot overlap 09:30-10:30. It either
        # ends at 09:30 (placed before the meeting) or starts at 10:30.
        # With a 09:00 start-of-day and 1h effort, it would naturally
        # fit 09:00-10:00 but that overlaps 09:30-10:30 -> the
        # scheduler pushes it to start after the meeting.
        wrow = by_id[work.id]
        assert wrow.scheduled_start is not None
        assert wrow.scheduled_end is not None
        # The plain work must NOT overlap the meeting window.
        assert not (
            wrow.scheduled_start < appt.scheduled_end and wrow.scheduled_end > appt.scheduled_start
        ), (
            f"collab work {wrow.scheduled_start}..{wrow.scheduled_end} "
            f"overlaps meeting {appt.scheduled_start}..{appt.scheduled_end}"
        )
