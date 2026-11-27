"""A task waits for where its predecessors really are in the leveled plan
(task be2fff43).

Before this change only the agent pool read a predecessor's leveled
window, and it read it the same way for every dependency type. A person's
task started at ``max(CPM earliest start, the person's cursor)``: the CPM
earliest start assumes infinite resources, so when a predecessor was
pushed back by its own person's queue the successor did not see it, and
the leveled plan had work starting before the work it depends on was
done. A task off every timeline (no estimate, so placed as a point) kept its
CPM window outright.

Each test below was run against the scheduler before the change and seen
failing; the failure is recorded beside it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from tests_helpers import seed_ai_assistant_identity

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.dependency import DependencyType
from mycelium_core.services import dependencies as deps
from mycelium_core.services import memberships, tasks
from mycelium_core.services import scheduler as sch
from mycelium_core.services.auth import signup

_RM = ZoneInfo("Europe/Rome")
# Monday 2026-01-12 09:00 Europe/Rome; the default calendar works 09-13
# and 14-18, Monday to Friday.
_AS_OF = dt.datetime(2026, 1, 12, 8, 0, tzinfo=dt.UTC)


def _loc(d: dt.datetime | None) -> tuple[int, int, int, int]:
    assert d is not None
    x = d.astimezone(_RM)
    return (x.month, x.day, x.hour, x.minute)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _two_people(name: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A workspace with its owner and a second member."""
    other_email = _email()
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name=name)
        b = await signup(s, email=other_email, password="pw-strong-123", org_name=name + "-b")
    async with tenant_session(str(a.org_id), str(a.user_id)) as s:
        await memberships.add_member(
            s, org_id=a.org_id, actor_id=a.user_id, email=other_email, role="member"
        )
    return a.org_id, a.user_id, b.user_id


async def test_a_persons_task_waits_for_the_leveled_finish_of_anothers() -> None:
    """Person 1 has 8 h of more important work before A, so A runs on
    Tuesday 09-11. B belongs to person 2, who is free, and depends on A.

    Before the change: B was placed at A's CPM finish, Monday 11:00, a day
    before A is done, and this test failed on the first assertion.
    """
    org, p1, p2 = await _two_people("XP")
    async with tenant_session(str(org), str(p1)) as s:
        first = await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="Person 1 first",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal(8),
            assignee_ids=[p1],
        )
        a = await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="A",
            importance=4,
            urgency=4,
            estimate_effort_h=Decimal(2),
            assignee_ids=[p1],
        )
        b = await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="B",
            estimate_effort_h=Decimal(2),
            assignee_ids=[p2],
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=p1,
            predecessor_id=a.id,
            successor_id=b.id,
            type=DependencyType.FS,
        )
        await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert _loc(rows[first.id].scheduled_end) == (1, 12, 18, 0)
    assert _loc(rows[a.id].scheduled_start) == (1, 13, 9, 0)
    assert _loc(rows[a.id].scheduled_end) == (1, 13, 11, 0)
    r_a, r_b = rows[a.id], rows[b.id]
    assert r_b.scheduled_start is not None and r_a.scheduled_end is not None
    assert r_b.scheduled_start >= r_a.scheduled_end
    assert _loc(r_b.scheduled_start) == (1, 13, 11, 0)
    assert _loc(r_b.scheduled_end) == (1, 13, 13, 0)


async def test_a_successor_on_no_timeline_moves_with_its_predecessor() -> None:
    """U has no estimate, so it is placed as a point and on nobody's
    timeline; it still cannot happen before the task it depends on is done.

    A first version of this test used a task with no assignee and passed
    before the change: ``create_task`` assigns the creator by default, so
    that task was on person 1's timeline after all and the test measured
    nothing. A task with no estimate is the case that really is off every
    timeline.

    Before the change: U kept its CPM window, Monday 11:00, and this test
    failed on the first assertion.
    """
    org, p1, _p2 = await _two_people("UN")
    async with tenant_session(str(org), str(p1)) as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="Person 1 first",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal(8),
            assignee_ids=[p1],
        )
        a = await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="A",
            estimate_effort_h=Decimal(2),
            assignee_ids=[p1],
        )
        u = await tasks.create_task(s, org_id=org, actor_id=p1, title="No estimate")
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=p1,
            predecessor_id=a.id,
            successor_id=u.id,
            type=DependencyType.FS,
        )
        await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert rows[u.id].scheduled_start == rows[a.id].scheduled_end
    assert _loc(rows[u.id].scheduled_start) == (1, 13, 11, 0)
    assert rows[u.id].dates_lower_bound is True


async def test_finish_to_finish_holds_the_finish_not_the_start() -> None:
    """An agent task L that must finish no earlier than a person's task H
    (FF). It may run so that it ends when H ends; it does not have to wait
    for H to end before it starts.

    Before the change: the agent pool read FF like FS and started L at H's
    end, so L ended an hour after H, and this test failed on the first
    assertion.
    """
    org, p1, _p2 = await _two_people("FF")
    async with tenant_session(str(org), str(p1)) as s:
        ai = await seed_ai_assistant_identity(s, org_id=org, user_id=p1)
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="Person 1 first",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal(8),
            assignee_ids=[p1],
        )
        h = await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="H",
            estimate_effort_h=Decimal(2),
            assignee_ids=[p1],
        )
        llm = await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="L",
            estimate_effort_h=Decimal(1),
            assignee_id=ai.id,
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=p1,
            predecessor_id=h.id,
            successor_id=llm.id,
            type=DependencyType.FF,
        )
        await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    r_h, r_l = rows[h.id], rows[llm.id]
    assert r_l.scheduled_end == r_h.scheduled_end
    assert r_h.scheduled_end is not None
    assert r_l.scheduled_start == r_h.scheduled_end - dt.timedelta(hours=1)


async def test_waiting_work_leaves_the_free_morning_to_other_work() -> None:
    """Person 2's important task B waits for A until Tuesday 11:00; their
    unrelated task C can still be done on Monday morning.

    Written as an attack on the single placement pass while it was being
    designed: with one cursor per person, B placed first would have moved
    the cursor to Tuesday and C would have gone after it, leaving Monday
    empty. Perturbed on 2026-11-27 so that ``_Timeline.place`` started after
    the last booked interval: C went to Tuesday 14:00 and this test failed.
    """
    org, p1, p2 = await _two_people("BF")
    async with tenant_session(str(org), str(p1)) as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="Person 1 first",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal(8),
            assignee_ids=[p1],
        )
        # A outranks C, so A is placed before C, B becomes ready, and B
        # (the most important) is placed before C too. Tasks created in one
        # transaction share ``created_at``; without distinct priorities the
        # order between A and C fell to their ids, C was sometimes placed
        # before B, and the first version of this test passed under the
        # perturbation below.
        a = await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="A",
            importance=2,
            urgency=2,
            estimate_effort_h=Decimal(2),
            assignee_ids=[p1],
        )
        b = await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="B",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal(2),
            assignee_ids=[p2],
        )
        c = await tasks.create_task(
            s, org_id=org, actor_id=p1, title="C", estimate_effort_h=Decimal(2), assignee_ids=[p2]
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=p1,
            predecessor_id=a.id,
            successor_id=b.id,
            type=DependencyType.FS,
        )
        await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert _loc(rows[b.id].scheduled_start) == (1, 13, 11, 0)
    assert _loc(rows[c.id].scheduled_start) == (1, 12, 9, 0)
    assert _loc(rows[c.id].scheduled_end) == (1, 12, 11, 0)
