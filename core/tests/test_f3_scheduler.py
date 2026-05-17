"""F3 deterministic scheduler (DB-backed), ADR-0004 verification.

Covers the plan's F3 acceptance bullets: two human tasks of the same
assignee without a dependency do not overlap; an LLM-delegated task is
parallel; SS + working-day lag across a holiday yields exact dates; an
in-progress task schedules only the residual from actual_start; same
input -> identical schedule; a manual pin survives an unrelated
recompute.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from flow_core.db import admin_session, tenant_session
from flow_core.models.dependency import DependencyType
from flow_core.models.task import ExecKind, ScheduleMode
from flow_core.services import calendar as cal_svc
from flow_core.services import dependencies as deps
from flow_core.services import scheduler as sch
from flow_core.services import tasks
from flow_core.services.auth import signup

_RM = ZoneInfo("Europe/Rome")
# Monday 2026-01-12 09:00 Europe/Rome (winter = UTC+1).
_AS_OF = dt.datetime(2026, 1, 12, 8, 0, tzinfo=dt.UTC)


def _loc(d: dt.datetime) -> tuple[int, int, int, int]:
    x = d.astimezone(_RM)
    return (x.month, x.day, x.hour, x.minute)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_scheduler_adr0004_core_scenarios() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="SCH")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        # A: two human tasks, same assignee, no dependency -> serialized.
        # Distinct priority makes the order deterministic by the rule
        # itself (P1 before P4), not by the uuid tie-break.
        h1 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="H1",
            priority=1,
            estimate_effort_h=Decimal(4),
            assignee_ids=[user],
        )
        h2 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="H2",
            priority=4,
            estimate_effort_h=Decimal(4),
            assignee_ids=[user],
        )
        # B: an LLM-delegated task (no assignee) is parallel.
        ai = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="AI",
            estimate_effort_h=Decimal(4),
            executor_kind=ExecKind.llm_agent,
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    s1, s2, sa = rows[h1.id], rows[h2.id], rows[ai.id]
    # P1 (h1) is serialized first, then P4 (h2): contiguous,
    # non-overlapping, both on working instants (lunch is skipped).
    assert _loc(s1.scheduled_start) == (1, 12, 9, 0)
    assert _loc(s1.scheduled_end) == (1, 12, 13, 0)
    assert _loc(s2.scheduled_start) == (1, 12, 14, 0)
    assert _loc(s2.scheduled_end) == (1, 12, 18, 0)
    assert s1.scheduled_end <= s2.scheduled_start  # disjoint
    # LLM task starts in parallel with the first human task, not after.
    assert _loc(sa.scheduled_start) == (1, 12, 9, 0)
    assert sa.scheduled_start == s1.scheduled_start


async def test_scheduler_ss_lag_across_holiday_exact_dates() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="SS")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        cal = await cal_svc.get_default_calendar(s, org)
        # Wednesday 2026-01-14 is a holiday on the default calendar.
        await cal_svc.add_holiday(
            s,
            org_id=org,
            actor_id=user,
            calendar_id=cal.id,
            day=dt.date(2026, 1, 14),
        )
        pred = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="PRED",
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        succ = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="SUCC",
            estimate_effort_h=Decimal(1),
            assignee_ids=[user],
        )
        # SS + 1320 working minutes (8h/day calendar): Mon(480) Tue(480)
        # Wed=holiday(skipped) leaves 360; Thu 09:00->13:00 (240) then
        # 14:00 +120 -> Thu 16:00.
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=pred.id,
            successor_id=succ.id,
            type=DependencyType.SS,
            lag_working_minutes=1320,
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert _loc(rows[pred.id].es) == (1, 12, 9, 0)  # Mon 09:00
    # The holiday pushes the SS target from Wed to Thu (exactly +1 day).
    assert _loc(rows[succ.id].es) == (1, 15, 16, 0)  # Thu 16:00


async def test_scheduler_in_progress_residual_from_actual_start() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ACT")
    org, user = a.org_id, a.user_id
    actual = dt.datetime(2026, 1, 13, 8, 0, tzinfo=dt.UTC)  # Tue 09:00 RM

    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="WIP",
            estimate_effort_h=Decimal(4),
            assignee_ids=[user],
        )
        # In progress: actual_start in the future of as_of, only 1h left.
        await tasks.set_schedule_fields(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            expected_version=t.version,
            values={"actual_start": actual, "remaining_effort_h": Decimal(1)},
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        r = await sch.get_schedule(s, org_id=org, task_id=t.id)

    assert r is not None
    assert _loc(r.es) == (1, 13, 9, 0)  # ES honours actual_start
    assert _loc(r.ef) == (1, 13, 10, 0)  # only the 1h residual, not 4h
    assert _loc(r.scheduled_start) == (1, 13, 9, 0)
    assert _loc(r.scheduled_end) == (1, 13, 10, 0)


async def test_scheduler_is_deterministic() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="DET")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        x = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="X",
            estimate_effort_h=Decimal(3),
            assignee_ids=[user],
        )
        y = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Y",
            estimate_effort_h=Decimal(5),
            assignee_ids=[user],
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=x.id,
            successor_id=y.id,
            type=DependencyType.FS,
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        first = {
            r.task_id: (r.es, r.ef, r.scheduled_start, r.scheduled_end, r.input_fingerprint)
            for r in await sch.list_schedule(s, org_id=org)
        }
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        second = {
            r.task_id: (r.es, r.ef, r.scheduled_start, r.scheduled_end, r.input_fingerprint)
            for r in await sch.list_schedule(s, org_id=org)
        }
    assert first == second
    assert len({fp for *_, fp in first.values()}) == 1


async def test_manual_pin_survives_unrelated_recompute() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="PIN")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        p1 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="P1",
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        p2 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="P2",
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        pinned = (await sch.get_schedule(s, org_id=org, task_id=p1.id)).scheduled_start

        # Public write-back path: mark p1 manual (its placement is pinned).
        await tasks.set_schedule_fields(
            s,
            org_id=org,
            actor_id=user,
            task_id=p1.id,
            expected_version=p1.version,
            values={"schedule_mode": ScheduleMode.manual},
        )
        # Unrelated change: p2 effort grows.
        await tasks.set_schedule_fields(
            s,
            org_id=org,
            actor_id=user,
            task_id=p2.id,
            expected_version=p2.version,
            values={"remaining_effort_h": Decimal(6)},
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        after = await sch.get_schedule(s, org_id=org, task_id=p1.id)

    assert after.scheduled_start == pinned
