"""A dependency out of a parent's start reads where its first subtask
starts, and three readings of the plan that had no test (task be2fff43,
reopened by its verification on 2c5fdf5b; task 27dc7e68).

A parent P was in the graph as a start node and a finish node. The start
node is the bound P's own predecessors put on its subtasks, and an SS or
SF out of P read it: when the subtasks were held later by their own
predecessors, or by their person's queue in the leveled plan, the
successor started before P did, while P's row said P started later.

Each test records beside it how it was seen failing: on 2c5fdf5b, or,
for a reading 2c5fdf5b already got right, under the mutation that breaks
it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.dependency import DependencyType
from mycelium_core.services import dependencies as deps
from mycelium_core.services import memberships, tasks
from mycelium_core.services import scheduler as sch
from mycelium_core.services.auth import signup
from mycelium_core.services.slack import slack_minutes

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


async def _task(
    s: AsyncSession,
    org: uuid.UUID,
    actor: uuid.UUID,
    title: str,
    hours: int | None,
    who: uuid.UUID,
    **kw: object,
) -> uuid.UUID:
    t = await tasks.create_task(
        s,
        org_id=org,
        actor_id=actor,
        title=title,
        estimate_effort_h=None if hours is None else Decimal(hours),
        assignee_ids=[who],
        **kw,  # type: ignore[arg-type]
    )
    return t.id


async def _dep(
    s: AsyncSession,
    org: uuid.UUID,
    actor: uuid.UUID,
    pred: uuid.UUID,
    succ: uuid.UUID,
    kind: DependencyType,
) -> None:
    await deps.add_dependency(
        s, org_id=org, actor_id=actor, predecessor_id=pred, successor_id=succ, type=kind
    )


async def test_start_to_start_out_of_a_parent_waits_for_its_first_subtask() -> None:
    """Z (4 h) comes before C, the only subtask of Epic, so Epic starts
    when C does, Monday 14:00. X depends start-to-start on Epic.

    On 2c5fdf5b: X's earliest start was Monday 09:00, the bound Epic's own
    (absent) predecessors put on it, and this test failed on the first
    assertion.
    """
    org, p1, p2 = await _two_people("PS-CPM")
    async with tenant_session(str(org), str(p1)) as s:
        z = await _task(s, org, p1, "Z", 4, p1)
        epic = await _task(s, org, p1, "Epic", None, p1)
        c = await _task(s, org, p1, "C", 2, p1, parent_task_id=epic)
        x = await _task(s, org, p1, "X", 2, p2)
        await _dep(s, org, p1, z, c, DependencyType.FS)
        await _dep(s, org, p1, epic, x, DependencyType.SS)
        summary = await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert _loc(rows[x].es) == (1, 12, 14, 0)
    assert rows[epic].es == rows[c].es == rows[x].es
    assert rows[x].scheduled_start is not None and rows[epic].scheduled_start is not None
    assert rows[x].scheduled_start >= rows[epic].scheduled_start
    # The latest start of the span is a start, like its earliest one.
    assert rows[epic].ls >= rows[epic].es
    assert summary.dependencies_ignored == 0


async def test_in_the_leveled_plan_too() -> None:
    """Person 1 has 8 h of more important work, so C, the only subtask of
    Epic, runs on Tuesday from 09:00. X, of person 2 who is free, depends
    start-to-start on Epic.

    On 2c5fdf5b: X was placed on Monday 09:00, a day before Epic starts,
    and this test failed on the first assertion.
    """
    org, p1, p2 = await _two_people("PS-LEV")
    async with tenant_session(str(org), str(p1)) as s:
        await _task(s, org, p1, "First", 8, p1, importance=1, urgency=1)
        epic = await _task(s, org, p1, "Epic", None, p1)
        c = await _task(s, org, p1, "C", 2, p1, parent_task_id=epic)
        x = await _task(s, org, p1, "X", 1, p2)
        await _dep(s, org, p1, epic, x, DependencyType.SS)
        await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert _loc(rows[x].scheduled_start) == (1, 13, 9, 0)
    assert rows[epic].scheduled_start == rows[c].scheduled_start == rows[x].scheduled_start


async def test_a_subtask_after_the_parents_successor_is_an_ordinary_plan() -> None:
    """The review starts with the epic, and step 2 of the epic waits for
    the review. Not a contradiction: step 1 starts the epic. Nothing is
    left out, the review starts with step 1 and step 2 follows the review.

    Seen failing with every subtask counted as a candidate first (the loop
    step 2 -> first(Epic) -> review -> step 2 closed): the review's
    dependency was left out and counted.
    """
    org, p1, p2 = await _two_people("PS-LOOP")
    async with tenant_session(str(org), str(p1)) as s:
        epic = await _task(s, org, p1, "Epic", None, p1)
        step1 = await _task(s, org, p1, "Step 1", 2, p1, parent_task_id=epic)
        step2 = await _task(s, org, p1, "Step 2", 2, p1, parent_task_id=epic)
        review = await _task(s, org, p1, "Review", 2, p2)
        await _dep(s, org, p1, epic, review, DependencyType.SS)
        await _dep(s, org, p1, review, step2, DependencyType.FS)
        summary = await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert summary.dependencies_ignored == 0
    assert rows[review].es == rows[step1].es == rows[epic].es
    assert rows[step2].es >= rows[review].ef


async def test_a_loop_through_two_parents_firsts_loses_no_dependency() -> None:
    """X starts with epic A and step 1 of epic B waits for X; Y starts with
    B and step 1 of A waits for Y. Through the two firsts this is a loop in
    the graph, but not a contradiction: step 2 of each epic starts it.

    Seen failing with the loop resolved by dropping a declared dependency,
    as a contradiction is: ``dependencies_ignored`` was 1.
    """
    org, p1, p2 = await _two_people("PS-TWO")
    async with tenant_session(str(org), str(p1)) as s:
        a = await _task(s, org, p1, "A", None, p1)
        a1 = await _task(s, org, p1, "A1", 2, p1, parent_task_id=a)
        a2 = await _task(s, org, p1, "A2", 2, p1, parent_task_id=a)
        b = await _task(s, org, p1, "B", None, p2)
        b1 = await _task(s, org, p1, "B1", 2, p2, parent_task_id=b)
        b2 = await _task(s, org, p1, "B2", 2, p2, parent_task_id=b)
        x = await _task(s, org, p1, "X", 1, p2)
        y = await _task(s, org, p1, "Y", 1, p1)
        await _dep(s, org, p1, a, x, DependencyType.SS)
        await _dep(s, org, p1, x, b1, DependencyType.FS)
        await _dep(s, org, p1, b, y, DependencyType.SS)
        await _dep(s, org, p1, y, a1, DependencyType.FS)
        summary = await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert summary.dependencies_ignored == 0
    assert rows[x].es == rows[a].es == rows[a2].es
    assert rows[y].es == rows[b].es == rows[b2].es
    assert rows[a1].es >= rows[y].ef
    assert rows[b1].es >= rows[x].ef


async def test_a_parent_cannot_start_after_its_own_successor_starts() -> None:
    """The review starts with the epic, and the epic's only subtask waits
    for the review: the epic would start after a task that starts with it.
    That dependency is left out and counted, and the plan is computed.

    On 2c5fdf5b: nothing was counted, since the review read the epic's
    start bound rather than its first subtask, and this test failed on the
    first assertion.
    """
    org, p1, p2 = await _two_people("PS-CONTRA")
    async with tenant_session(str(org), str(p1)) as s:
        epic = await _task(s, org, p1, "Epic", None, p1)
        only = await _task(s, org, p1, "Only", 2, p1, parent_task_id=epic)
        review = await _task(s, org, p1, "Review", 2, p2)
        await _dep(s, org, p1, epic, review, DependencyType.SS)
        await _dep(s, org, p1, review, only, DependencyType.FS)
        summary = await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert summary.dependencies_ignored == 1
    assert rows[only].es >= rows[review].ef


async def test_finish_to_finish_into_a_parent_holds_its_finish() -> None:
    """Y (8 h) is finish-to-finish into Epic, whose only subtask C takes 2 h:
    Epic cannot end before Y does, and C is not held back.

    Passes on 2c5fdf5b. Seen failing with FF and SF into a parent attached
    to its start (C held, Epic ending with C at 11:00).
    """
    org, p1, p2 = await _two_people("PS-FF")
    async with tenant_session(str(org), str(p1)) as s:
        y = await _task(s, org, p1, "Y", 8, p2)
        epic = await _task(s, org, p1, "Epic", None, p1)
        c = await _task(s, org, p1, "C", 2, p1, parent_task_id=epic)
        await _dep(s, org, p1, y, epic, DependencyType.FF)
        await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert _loc(rows[c].es) == (1, 12, 9, 0)
    assert rows[epic].ef == rows[y].ef
    assert _loc(rows[epic].ef) == (1, 12, 18, 0)


async def test_start_to_finish_latest_start_reads_the_successors_finish() -> None:
    """A is start-to-finish into B: B cannot finish before A starts, so A
    may start as late as B's latest finish. The 8 h task ends the plan on
    Monday at 18:00, which is B's latest finish, and A (2 h) has until
    16:00 to start.

    Passes on 2c5fdf5b. Seen failing with the backward pass reading B's
    latest START (14:00) instead, which gave A a latest start of 14:00.
    """
    org, p1, p2 = await _two_people("PS-SF")
    async with tenant_session(str(org), str(p1)) as s:
        await _task(s, org, p1, "Long", 8, p2)
        a = await _task(s, org, p1, "A", 2, p1)
        b = await _task(s, org, p1, "B", 4, p1)
        await _dep(s, org, p1, a, b, DependencyType.SF)
        await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert _loc(rows[b].lf) == (1, 12, 18, 0)
    assert _loc(rows[a].ls) == (1, 12, 16, 0)


async def test_the_due_margin_is_measured_on_the_leveled_finish() -> None:
    """T (2 h) is due Monday 18:00, and its person has 8 h of more
    important work first: T could finish at 11:00 on infinite resources,
    but is placed on Tuesday 09-11, so it misses its date.

    Passes on 2c5fdf5b. Seen failing with the margin taken on the CPM
    finish (+420 minutes, a date met that is not).
    """
    org, p1, _ = await _two_people("PS-DUE")
    due = dt.datetime(2026, 1, 12, 17, 0, tzinfo=dt.UTC)
    async with tenant_session(str(org), str(p1)) as s:
        await _task(s, org, p1, "First", 8, p1, importance=1, urgency=1)
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=p1,
            title="T",
            estimate_effort_h=Decimal(2),
            assignee_ids=[p1],
            due_date=due,
        )
        await sch.recompute(s, org_id=org, actor_id=p1, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    r = rows[t.id]
    assert _loc(r.scheduled_end) == (1, 13, 11, 0)
    assert r.scheduled_end is not None
    assert r.due_margin_minutes == slack_minutes(due, r.scheduled_end)
    assert r.due_margin_minutes is not None and r.due_margin_minutes < 0
