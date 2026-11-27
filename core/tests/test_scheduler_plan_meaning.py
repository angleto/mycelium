"""The plan's dates mean what they say (task 27dc7e68).

Measured on production on 2026-11-26, before this change:

- a parent's own estimate was scheduled on top of its subtasks, so the
  one critical task in the workspace was an epic of 624 h;
- a task with no estimate was scheduled as 0 minutes, silently: 1348 of
  1605 rows had zero duration;
- a due date was only a sort key, so no row said whether its task would
  finish by it;
- 6 rows had a latest start after their latest finish.

Each test below was run against the scheduler as it was before the change
and seen failing; the failure is recorded beside it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession
from tests_helpers import seed_ai_assistant_identity

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.dependency import DependencyType
from mycelium_core.services import dependencies as deps
from mycelium_core.services import scheduler as sch
from mycelium_core.services import tasks
from mycelium_core.services.auth import signup
from mycelium_core.services.slack import slack_minutes

_RM = ZoneInfo("Europe/Rome")
# Monday 2026-01-12 09:00 Europe/Rome (winter = UTC+1), the start of the
# default calendar's first window (09-13, 14-18, Monday to Friday).
_AS_OF = dt.datetime(2026, 1, 12, 8, 0, tzinfo=dt.UTC)


def _loc(d: dt.datetime) -> tuple[int, int, int, int]:
    x = d.astimezone(_RM)
    return (x.month, x.day, x.hour, x.minute)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _workspace(name: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name=name)
    return a.org_id, a.user_id


async def _milestone(s: AsyncSession, *, org: uuid.UUID, user: uuid.UUID, title: str) -> uuid.UUID:
    t = await tasks.create_task(s, org_id=org, actor_id=user, title=title)
    await tasks.set_schedule_fields(
        s,
        org_id=org,
        actor_id=user,
        task_id=t.id,
        expected_version=t.version,
        values={"is_milestone": True},
    )
    return t.id


async def test_latest_start_is_never_after_latest_finish() -> None:
    """A zero-length task whose latest finish falls on the end of a working
    window had its latest start moved to the next morning: the backward pass
    computed it as "add 0 minutes", which snaps forward. The long task here
    ends at 18:00 on Monday, so the plan ends then too.

    Before the change: ``ls`` of the zero-length task was Tuesday 09:00 and
    its ``lf`` Monday 18:00, and this test failed on the first assertion.
    """
    org, user = await _workspace("LSLF")
    async with tenant_session(str(org), str(user)) as s:
        await tasks.create_task(
            s, org_id=org, actor_id=user, title="Long", estimate_effort_h=Decimal(8)
        )
        await _milestone(s, org=org, user=user, title="Point")
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = await sch.list_schedule(s, org_id=org)

    assert rows
    for r in rows:
        assert r.ls <= r.lf, (r.task_id, r.ls, r.lf)
        assert r.es <= r.ef, (r.task_id, r.es, r.ef)


async def test_parent_effort_is_its_subtasks_and_it_spans_them() -> None:
    """Shaped like the 624 h epic: a parent with an estimate of its own, two
    subtasks, a predecessor of the parent and a successor of the parent.

    The parent's own 100 h is not work to schedule: its subtasks are. The
    predecessor of the parent holds back the subtasks, the successor of the
    parent waits for the last subtask, and the critical path runs through
    the longer subtask, not through the parent's estimate.

    Before the change: the successor started after the parent's own 100
    working hours (on Thursday 2026-01-29), the subtasks started at 09:00
    regardless of the parent's predecessor, and this test failed on the
    first assertion.
    """
    org, user = await _workspace("EPIC")
    async with tenant_session(str(org), str(user)) as s:
        before = await tasks.create_task(
            s, org_id=org, actor_id=user, title="Before", estimate_effort_h=Decimal(4)
        )
        epic = await tasks.create_task(
            s, org_id=org, actor_id=user, title="Epic", estimate_effort_h=Decimal(100)
        )
        long_child = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Long child",
            estimate_effort_h=Decimal(8),
            parent_task_id=epic.id,
        )
        short_child = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Short child",
            estimate_effort_h=Decimal(2),
            parent_task_id=epic.id,
        )
        after = await tasks.create_task(
            s, org_id=org, actor_id=user, title="After", estimate_effort_h=Decimal(2)
        )
        for pred, succ in ((before.id, epic.id), (epic.id, after.id)):
            await deps.add_dependency(
                s,
                org_id=org,
                actor_id=user,
                predecessor_id=pred,
                successor_id=succ,
                type=DependencyType.FS,
            )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    p, lc, sc, a = rows[epic.id], rows[long_child.id], rows[short_child.id], rows[after.id]
    # "Before" runs 09-13 on Monday; the subtasks start after it.
    assert _loc(a.es) == (1, 13, 14, 0)
    assert _loc(lc.es) == (1, 12, 14, 0)
    assert _loc(sc.es) == (1, 12, 14, 0)
    # The long subtask runs Monday 14-18 and Tuesday 09-13.
    assert _loc(lc.ef) == (1, 13, 13, 0)
    # The parent spans its subtasks: it starts with the first and ends with
    # the last, and its own estimate adds nothing.
    assert p.es == lc.es
    assert p.ef == lc.ef
    # In the leveled plan too: the subtasks share one person, so the
    # parent's window is the span of both.
    assert p.scheduled_start == min(lc.scheduled_start, sc.scheduled_start)
    assert p.scheduled_end == max(lc.scheduled_end, sc.scheduled_end)
    # The critical path follows the subtasks.
    assert lc.on_logical_critical_path is True
    assert sc.on_logical_critical_path is False
    assert (sc.slack_minutes or 0) > 0
    assert p.on_logical_critical_path is True
    assert rows[before.id].on_logical_critical_path is True


async def test_missing_estimate_is_unknown_and_counted_never_zero() -> None:
    """A task with no estimate is not a task of zero minutes. Its row says
    the estimate is missing, every date that depends on it is a lower bound,
    and the summary counts it.

    Before the change: the summary had no count and the rows had no flag,
    and this test failed on the first assertion with an AttributeError.
    """
    org, user = await _workspace("UNK")
    async with tenant_session(str(org), str(user)) as s:
        unknown = await tasks.create_task(s, org_id=org, actor_id=user, title="No estimate")
        after = await tasks.create_task(
            s, org_id=org, actor_id=user, title="After it", estimate_effort_h=Decimal(2)
        )
        apart = await tasks.create_task(
            s, org_id=org, actor_id=user, title="Unrelated", estimate_effort_h=Decimal(1)
        )
        point = await _milestone(s, org=org, user=user, title="Milestone")
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=unknown.id,
            successor_id=after.id,
            type=DependencyType.FS,
        )
        summary = await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert summary.unestimated_count == 1
    assert rows[unknown.id].estimate_missing is True
    assert rows[unknown.id].dates_lower_bound is True
    assert rows[after.id].estimate_missing is False
    assert rows[after.id].dates_lower_bound is True
    assert rows[apart.id].dates_lower_bound is False
    # A milestone has no duration by definition: nothing is missing.
    assert rows[point].estimate_missing is False
    assert rows[point].dates_lower_bound is False


async def test_due_date_is_a_finish_constraint_with_its_margin() -> None:
    """A due date bounds the latest finish of its task and of everything
    before it. Each row says by how much its task meets or misses its date,
    the slack is negative where it is missed, and the summary counts the
    misses.

    Before the change: the due date did not enter the backward pass, the
    slack was clamped at 0, and this test failed on the first assertion
    with an AttributeError.
    """
    org, user = await _workspace("DUE")
    async with tenant_session(str(org), str(user)) as s:
        first = await tasks.create_task(
            s, org_id=org, actor_id=user, title="First", estimate_effort_h=Decimal(2)
        )
        # Due at 13:00 on Monday, but it cannot start before 11:00 and lasts
        # 4 h: it ends at 16:00, three hours late.
        late = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Late",
            estimate_effort_h=Decimal(4),
            due_date=dt.datetime(2026, 1, 12, 12, 0, tzinfo=dt.UTC),
        )
        easy_due = dt.datetime(2026, 1, 16, 17, 0, tzinfo=dt.UTC)
        easy = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Easy",
            estimate_effort_h=Decimal(2),
            due_date=easy_due,
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=first.id,
            successor_id=late.id,
            type=DependencyType.FS,
        )
        summary = await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert summary.due_missed_count == 1
    r_late, r_easy, r_first = rows[late.id], rows[easy.id], rows[first.id]
    assert _loc(r_late.ef) == (1, 12, 16, 0)
    assert r_late.due_margin_minutes == -180
    assert r_late.slack_minutes == -180
    assert r_late.on_logical_critical_path is True
    # The miss propagates back to the predecessor: it would have to finish
    # three working hours earlier than it can.
    assert r_first.slack_minutes is not None and r_first.slack_minutes < 0
    assert r_first.due_margin_minutes is None
    assert r_easy.due_margin_minutes is not None and r_easy.due_margin_minutes > 0
    # With no successor, a task's slack is measured against its own date,
    # not against the end of the plan (Monday 16:00 here).
    assert r_easy.slack_minutes == slack_minutes(easy_due, r_easy.ef)


async def test_dependency_between_a_task_and_its_parent_is_reported_not_a_cycle() -> None:
    """A parent spans its subtasks, so a dependency between a task and one of
    its own ancestors cannot hold: the subtask would have to finish before
    the span that contains it starts. It is left out of the plan and counted,
    rather than failing the whole workspace as a cycle.

    Before the change: the summary had no such count, and this test failed
    with an AttributeError.
    """
    org, user = await _workspace("ANC")
    async with tenant_session(str(org), str(user)) as s:
        epic = await tasks.create_task(s, org_id=org, actor_id=user, title="Epic")
        child = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Child",
            estimate_effort_h=Decimal(2),
            parent_task_id=epic.id,
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=child.id,
            successor_id=epic.id,
            type=DependencyType.FS,
        )
        summary = await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert summary.dependencies_ignored == 1
    assert rows[epic.id].ef == rows[child.id].ef


async def test_nested_parent_and_a_start_dependency_read_the_right_end() -> None:
    """A span inside a span, and a start-to-start dependency out of the
    outer one. SS out of a parent reads the parent's START, which is its
    first subtask's start: the successor may begin as soon as the first
    piece of the epic does, not when the last one does. Copying the
    dependency onto every subtask would have held it to the last.

    Written after the change, as an attack on the claim in ``_plan_graph``
    that the attachment is exact; it passed on the first run. Perturbed on
    2026-11-27 so that SS out of a parent read the parent's finish (the
    copy-onto-every-subtask reading): it failed with the follower at 17:00
    instead of 10:00.
    """
    org, user = await _workspace("NEST")
    async with tenant_session(str(org), str(user)) as s:
        epic = await tasks.create_task(s, org_id=org, actor_id=user, title="Epic")
        story = await tasks.create_task(
            s, org_id=org, actor_id=user, title="Story", parent_task_id=epic.id
        )
        first = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="First",
            estimate_effort_h=Decimal(2),
            parent_task_id=story.id,
        )
        second = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Second",
            estimate_effort_h=Decimal(4),
            parent_task_id=epic.id,
        )
        follower = await tasks.create_task(
            s, org_id=org, actor_id=user, title="Follower", estimate_effort_h=Decimal(1)
        )
        # The second subtask waits for the first: the epic's span is 6 h.
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=first.id,
            successor_id=second.id,
            type=DependencyType.FS,
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=epic.id,
            successor_id=follower.id,
            type=DependencyType.SS,
            lag_working_minutes=60,
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    e, st, f, sc, fo = (
        rows[epic.id],
        rows[story.id],
        rows[first.id],
        rows[second.id],
        rows[follower.id],
    )
    assert _loc(f.es) == (1, 12, 9, 0)
    assert st.es == f.es and st.ef == f.ef
    assert e.es == f.es and e.ef == sc.ef
    assert _loc(e.ef) == (1, 12, 16, 0)
    # One working hour after the epic's first piece starts.
    assert _loc(fo.es) == (1, 12, 10, 0)
    # Neither parent has an estimate, and nothing is missing: a parent's
    # effort is its subtasks'.
    assert e.estimate_missing is False and e.dates_lower_bound is False


async def test_an_agent_task_with_no_estimate_is_a_flagged_dispatch_gap() -> None:
    """An agent task with no estimate cannot be admitted: admission checks
    the agent's credit budget against effort times rate, and with no effort
    that check would pass whatever the task costs. It is a dispatch gap with
    its own reason, which tells the owner what to do about it.

    Before the change (task 238a76da): the task went off every timeline
    with no executor and was NOT flagged, so the dispatch tick never took
    it and nothing said why; this test failed on the first assertion.
    """
    org, user = await _workspace("AGU")
    async with tenant_session(str(org), str(user)) as s:
        ai = await seed_ai_assistant_identity(s, org_id=org, user_id=user)
        task = await tasks.create_task(
            s, org_id=org, actor_id=user, title="For the agent", assignee_id=ai.id
        )
        summary = await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        row = await sch.get_schedule(s, org_id=org, task_id=task.id)

    assert row is not None
    assert row.unassignable is True
    assert row.unassignable_reason == "estimate_missing"
    assert row.estimate_missing is True
    assert row.assigned_executor_id is None
    assert row.scheduled_start is None
    assert summary.unassignable_count == 1
    assert summary.unestimated_count == 1
