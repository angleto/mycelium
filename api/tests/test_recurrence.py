"""Recurrence engine (migration 0094 + services/recurrence.py).

When a task with a ``recurrence`` spec transitions into a terminal
workflow state, the engine spawns the next occurrence: a fresh task
in the initial state with ``start_at`` (or ``due_date``) shifted
forward per the spec, all other relevant columns cloned.

Covers:
- date helpers: daily, weekly w/ by_weekday, monthly w/ clamping,
  yearly w/ Feb 29 clamp, ``until`` end-of-chain;
- spawn on done for an appointment-task (start_at shift);
- spawn on done for a reminder (due_date shift);
- chain ends past ``until`` -> no spawn;
- extra participants are mirrored onto the next occurrence.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.task import Task
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.models.task_participant import TaskParticipant
from mycelium_core.models.user import User
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import actors as actors_svc
from mycelium_core.services import identities as identities_svc
from mycelium_core.services import memberships as mem_svc
from mycelium_core.services import participants as p_svc
from mycelium_core.services import recurrence as rec
from mycelium_core.services import tasks
from mycelium_core.services.auth import signup


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def test_next_date_daily() -> None:
    d = rec.next_occurrence_date(dt.date(2026, 1, 1), {"kind": "daily"})
    assert d == dt.date(2026, 1, 2)
    d = rec.next_occurrence_date(dt.date(2026, 1, 1), {"kind": "daily", "interval": 3})
    assert d == dt.date(2026, 1, 4)


def test_next_date_weekly_by_weekday() -> None:
    # Anchor = Mon 2026-01-05. by_weekday=[mon,wed,fri]. Next = Wed 07.
    d = rec.next_occurrence_date(
        dt.date(2026, 1, 5),
        {"kind": "weekly", "by_weekday": ["mon", "wed", "fri"]},
    )
    assert d == dt.date(2026, 1, 7)
    # Anchor = Fri 2026-01-09 -> wrap to next Mon (12).
    d = rec.next_occurrence_date(
        dt.date(2026, 1, 9),
        {"kind": "weekly", "by_weekday": ["mon", "wed", "fri"]},
    )
    assert d == dt.date(2026, 1, 12)


def test_next_date_monthly_clamps_to_last_day() -> None:
    # Jan 31 + 1 month with anchor day 31 -> clamp to Feb 28 (2026).
    d = rec.next_occurrence_date(dt.date(2026, 1, 31), {"kind": "monthly", "interval": 1})
    assert d == dt.date(2026, 2, 28)
    # Explicit by_month_day=15.
    d = rec.next_occurrence_date(
        dt.date(2026, 1, 31),
        {"kind": "monthly", "by_month_day": 15},
    )
    assert d == dt.date(2026, 2, 15)


def test_next_date_yearly_feb29_clamps() -> None:
    d = rec.next_occurrence_date(dt.date(2028, 2, 29), {"kind": "yearly"})
    assert d == dt.date(2029, 2, 28)


def test_until_ends_chain() -> None:
    d = rec.next_occurrence_date(
        dt.date(2026, 12, 30),
        {"kind": "daily", "until": "2026-12-31"},
    )
    assert d == dt.date(2026, 12, 31)
    # One step past the end: chain stops.
    d = rec.next_occurrence_date(
        dt.date(2026, 12, 31),
        {"kind": "daily", "until": "2026-12-31"},
    )
    assert d is None


def test_invalid_spec_rejected() -> None:
    from mycelium_core.errors import DomainError

    with pytest.raises(DomainError):
        rec.next_occurrence_date(dt.date(2026, 1, 1), {"kind": "bogus"})
    with pytest.raises(DomainError):
        rec.next_occurrence_date(dt.date(2026, 1, 1), {"kind": "daily", "interval": 0})
    with pytest.raises(DomainError):
        rec.next_occurrence_date(dt.date(2026, 1, 1), {"kind": "monthly", "by_month_day": 32})


async def _seed_user() -> tuple[uuid.UUID, uuid.UUID]:
    em = _email()
    async with admin_session() as s:
        a = await signup(s, email=em, password="pw-strong-123", org_name="R")
    return a.org_id, a.user_id


async def test_spawn_on_done_appointment_shifts_start_at() -> None:
    org, user = await _seed_user()
    async with tenant_session(str(org), str(user)) as s:
        await actors_svc.mint_user_handle(s, user_id=user, seed="u")
        ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=user)
        anchor_start = dt.datetime(2026, 9, 7, 9, 0, tzinfo=dt.UTC)
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Weekly standup",
            assignee_id=ident.id,
            start_at=anchor_start,
            duration_minutes=30,
            recurrence={"kind": "weekly"},
        )
        # Walk into the terminal state.
        states = {
            ws.name: ws
            for ws in (
                await s.execute(
                    select(WorkflowState)
                    .join(Task, Task.state_id == WorkflowState.id)
                    .where(Task.id == t.id)
                )
            )
            .scalars()
            .all()
        }
        # Look up all states of the task's workflow for the transitions.
        from mycelium_core.services import workflow as wf_svc

        wf = await wf_svc.effective_workflow_for_task(s, org, t.id)
        all_states = {
            ws.name: ws
            for ws in (
                await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == wf.id))
            )
            .scalars()
            .all()
        }
        _ = states  # silence
        # Walk via valid transitions (initial -> in_progress -> done).
        ver = t.version
        ver = await tasks.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            expected_version=ver,
            state_id=all_states["in_progress"].id,
        )
        ver = await tasks.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            expected_version=ver,
            state_id=all_states["done"].id,
        )
        # A second row must exist with start_at = anchor + 1 week.
        rows = (
            (
                await s.execute(
                    select(Task).where(
                        Task.title == "Weekly standup",
                        Task.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        spawned = [r for r in rows if r.id != t.id]
        assert len(spawned) == 1, [r.id for r in rows]
        assert spawned[0].start_at == anchor_start + dt.timedelta(weeks=1)
        assert spawned[0].duration_minutes == 30
        assert spawned[0].recurrence == {"kind": "weekly"}


async def test_spawn_on_done_reminder_shifts_due_date() -> None:
    org, user = await _seed_user()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Pay invoice",
            due_date=dt.datetime(2026, 1, 31, 23, 59, 59, tzinfo=dt.UTC),
            recurrence={"kind": "monthly"},
        )
        from mycelium_core.services import workflow as wf_svc

        wf = await wf_svc.effective_workflow_for_task(s, org, t.id)
        states = {
            ws.name: ws
            for ws in (
                await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == wf.id))
            )
            .scalars()
            .all()
        }
        ver = await tasks.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            expected_version=t.version,
            state_id=states["in_progress"].id,
        )
        await tasks.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            expected_version=ver,
            state_id=states["done"].id,
        )
        rows = (
            (
                await s.execute(
                    select(Task).where(
                        Task.title == "Pay invoice",
                        Task.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        spawned = [r for r in rows if r.id != t.id]
        assert len(spawned) == 1
        # Jan 31 + 1 month clamps to Feb 28 (2026 is not a leap year).
        # Migration 0005: due_date is timestamptz, time-of-day preserved.
        assert spawned[0].due_date == dt.datetime(2026, 2, 28, 23, 59, 59, tzinfo=dt.UTC)
        assert spawned[0].start_at is None
        assert spawned[0].duration_minutes is None


async def test_until_stops_the_chain() -> None:
    org, user = await _seed_user()
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Last reminder",
            due_date=dt.datetime(2026, 12, 31, 23, 59, 59, tzinfo=dt.UTC),
            recurrence={"kind": "daily", "until": "2026-12-31"},
        )
        from mycelium_core.services import workflow as wf_svc

        wf = await wf_svc.effective_workflow_for_task(s, org, t.id)
        states = {
            ws.name: ws
            for ws in (
                await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == wf.id))
            )
            .scalars()
            .all()
        }
        ver = await tasks.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            expected_version=t.version,
            state_id=states["in_progress"].id,
        )
        await tasks.set_state(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            expected_version=ver,
            state_id=states["done"].id,
        )
        rows = (await s.execute(select(Task).where(Task.title == "Last reminder"))).scalars().all()
        assert len(rows) == 1  # no spawn past the until date


async def test_spawn_carries_extra_participants() -> None:
    a_email = _email()
    b_email = _email()
    async with admin_session() as s:
        a = await signup(s, email=a_email, password="pw-strong-123", org_name="R")
        b = await signup(s, email=b_email, password="pw-strong-123", org_name="O")
    org, owner = a.org_id, a.user_id
    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=b_email, role="member")
        await actors_svc.mint_user_handle(s, user_id=owner, seed=a_email)
        await actors_svc.mint_user_handle(s, user_id=b.user_id, seed=b_email)
        owner_ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=owner)
        collab_ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=b.user_id)
        anchor_start = dt.datetime(2026, 9, 7, 10, 0, tzinfo=dt.UTC)
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Pairing session",
            assignee_id=owner_ident.id,
            start_at=anchor_start,
            duration_minutes=60,
            recurrence={"kind": "weekly"},
        )
        await p_svc.add_participant(
            s,
            org_id=org,
            actor_id=owner,
            task_id=t.id,
            identity_id=collab_ident.id,
        )
        from mycelium_core.services import workflow as wf_svc

        wf = await wf_svc.effective_workflow_for_task(s, org, t.id)
        states = {
            ws.name: ws
            for ws in (
                await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == wf.id))
            )
            .scalars()
            .all()
        }
        ver = await tasks.set_state(
            s,
            org_id=org,
            actor_id=owner,
            task_id=t.id,
            expected_version=t.version,
            state_id=states["in_progress"].id,
        )
        await tasks.set_state(
            s,
            org_id=org,
            actor_id=owner,
            task_id=t.id,
            expected_version=ver,
            state_id=states["done"].id,
        )
        # New occurrence: should have the collab as an EXTRA participant
        # (the trigger handles the assignee mirror).
        spawned = (
            await s.execute(
                select(Task).where(
                    Task.title == "Pairing session",
                    Task.start_at == anchor_start + dt.timedelta(weeks=1),
                )
            )
        ).scalar_one()
        participants = (
            (
                await s.execute(
                    select(TaskParticipant.identity_id).where(TaskParticipant.task_id == spawned.id)
                )
            )
            .scalars()
            .all()
        )
        assert set(participants) == {owner_ident.id, collab_ident.id}


async def test_spawn_drops_an_assignee_whose_principal_was_deactivated() -> None:
    """A spawn is a NEW assignment, not preserved history.

    The resolver refuses to bind a deactivated principal on every write
    path, but ``maybe_spawn_next`` builds its successor as a raw ORM row
    and never goes through it. Left alone, a recurring task would keep
    minting fresh, un-actionable occurrences for someone who cannot log
    in, forever. It now spawns UNASSIGNED, where the work is at least
    visible in the unassigned queue.

    The TEMPLATE keeps its assignee: deactivating hides a principal from
    the pickers, it never rewrites what they already hold.
    """
    owner_email = _email()
    bot_email = _email()
    async with admin_session() as s:
        a = await signup(s, email=owner_email, password="pw-strong-123", org_name="R")
        b = await signup(s, email=bot_email, password="pw-strong-123", org_name="O")
    org, owner = a.org_id, a.user_id
    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=bot_email, role="member")
        await actors_svc.mint_user_handle(s, user_id=owner, seed=owner_email)
        await actors_svc.mint_user_handle(s, user_id=b.user_id, seed=bot_email)
        leaver_ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=b.user_id)
        anchor_start = dt.datetime(2026, 9, 14, 10, 0, tzinfo=dt.UTC)
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Weekly handover",
            assignee_id=leaver_ident.id,
            start_at=anchor_start,
            duration_minutes=30,
            recurrence={"kind": "weekly"},
        )
        await tasks.assign(s, org_id=org, actor_id=owner, task_id=t.id, user_id=b.user_id)
        task_id, task_v = t.id, t.version

    async with admin_session() as s:
        await s.execute(update(User).where(User.id == b.user_id).values(is_active=False))

    async with tenant_session(str(org), str(owner)) as s:
        from mycelium_core.services import workflow as wf_svc

        wf = await wf_svc.effective_workflow_for_task(s, org, task_id)
        states = {
            ws.name: ws
            for ws in (
                await s.execute(select(WorkflowState).where(WorkflowState.workflow_id == wf.id))
            )
            .scalars()
            .all()
        }
        ver = await tasks.set_state(
            s,
            org_id=org,
            actor_id=owner,
            task_id=task_id,
            expected_version=task_v,
            state_id=states["in_progress"].id,
        )
        await tasks.set_state(
            s,
            org_id=org,
            actor_id=owner,
            task_id=task_id,
            expected_version=ver,
            state_id=states["done"].id,
        )
        spawned = (
            await s.execute(
                select(Task).where(
                    Task.title == "Weekly handover",
                    Task.start_at == anchor_start + dt.timedelta(weeks=1),
                )
            )
        ).scalar_one()
        assert spawned.assignee_id is None, "a new occurrence must not go to a dead handle"
        template = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        assert template.assignee_id == leaver_ident.id, "history is not rewritten"
        # Same rule one door over: the collaborator set is cloned onto the
        # new occurrence too, and cloning a dead one would write work at
        # one door that the reminder scan then discards at another.
        spawned_collabs = (
            (
                await s.execute(
                    select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == spawned.id)
                )
            )
            .scalars()
            .all()
        )
        assert b.user_id not in set(spawned_collabs)
        template_collabs = (
            (
                await s.execute(
                    select(TaskCollaborator.user_id).where(TaskCollaborator.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )
        assert b.user_id in set(template_collabs), "the template keeps its own history"


async def test_add_participant_by_identity_id_refuses_a_deactivated_principal() -> None:
    """The explicit-id door. ``add_participant`` accepts either a handle
    (resolved, hence gated) or a raw ``identity_id`` straight off the
    request body -- gating only the first would be the "one door
    filtered, the other five open" mistake."""
    owner_email = _email()
    guest_email = _email()
    async with admin_session() as s:
        a = await signup(s, email=owner_email, password="pw-strong-123", org_name="R")
        g = await signup(s, email=guest_email, password="pw-strong-123", org_name="O")
    org, owner = a.org_id, a.user_id
    async with tenant_session(str(org), str(owner)) as s:
        await mem_svc.add_member(s, org_id=org, actor_id=owner, email=guest_email, role="member")
        await actors_svc.mint_user_handle(s, user_id=owner, seed=owner_email)
        await actors_svc.mint_user_handle(s, user_id=g.user_id, seed=guest_email)
        guest_ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=g.user_id)
        guest_ident_id, guest_handle = guest_ident.id, guest_ident.handle
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=owner,
            title="Standup",
            start_at=dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC),
            duration_minutes=15,
        )
        task_id = t.id

    async with admin_session() as s:
        await s.execute(update(User).where(User.id == g.user_id).values(is_active=False))

    async with tenant_session(str(org), str(owner)) as s:
        with pytest.raises(DomainError) as by_id:
            await p_svc.add_participant(
                s, org_id=org, actor_id=owner, task_id=task_id, identity_id=guest_ident_id
            )
        assert by_id.value.code is MessageCode.IDENTITY_INACTIVE
    async with tenant_session(str(org), str(owner)) as s:
        with pytest.raises(DomainError) as by_handle:
            await p_svc.add_participant(
                s, org_id=org, actor_id=owner, task_id=task_id, handle=guest_handle
            )
        assert by_handle.value.code is MessageCode.IDENTITY_INACTIVE
