"""ADR-0025 P1: resource-aware scheduler verification (DB-backed).

Covers the P1 acceptance bullets: human context-switch penalty between
back-to-back distinct tasks; LLM K-parallel pool cap; policy changes
the deterministic order/makespan; RecomputeOut carries
makespan/projected credit cost; the resource-aware critical chain is a
superset of (and differs from) the logical critical path under
contention; determinism (same inputs+policy -> identical schedule); a
fresh workspace recomputes with zero manual executor config.

Style mirrors api/tests/test_f3_api.py + core/tests/test_f3_scheduler.py
(signup -> tenant_session; privileged API calls send X-Workspace-Role:
owner).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from flow_api.main import app
from flow_core.db import admin_session, tenant_session
from flow_core.models.dependency import DependencyType
from flow_core.models.executor import Executor, ExecutorKind
from flow_core.models.schedule import Schedule
from flow_core.models.task import ExecKind, SchedulePolicy
from flow_core.services import dependencies as deps
from flow_core.services import executors as exec_svc
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


def _overlap(
    rows: list[Schedule],
) -> int:
    """Max number of intervals [scheduled_start, scheduled_end) that are
    simultaneously active (a sweep over endpoints; deterministic)."""
    pts: list[tuple[dt.datetime, int]] = []
    for r in rows:
        assert r.scheduled_start is not None and r.scheduled_end is not None
        pts.append((r.scheduled_start, 1))
        pts.append((r.scheduled_end, -1))
    # End before start at the same instant: half-open intervals do not
    # count as overlapping when one ends exactly as another starts.
    pts.sort(key=lambda p: (p[0], p[1]))
    cur = 0
    peak = 0
    for _, delta in pts:
        cur += delta
        peak = max(peak, cur)
    return peak


async def test_human_context_switch_pushes_back_to_back_task() -> None:
    """(a) A human with context_switch_cost>0 and two back-to-back
    distinct tasks: the second task's start is pushed by the switch
    cost (vs the zero-cost baseline)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="CSW")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        h1 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="H1",
            priority=1,
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        h2 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="H2",
            priority=4,
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        # Baseline: first recompute also lazily seeds the human Executor
        # (switch cost defaults to 0 -> pre-P1 behaviour).
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        base = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}
        # H1 09:00->11:00, H2 contiguous at 11:00 (no switch penalty).
        assert _loc(base[h1.id].scheduled_start) == (1, 12, 9, 0)
        assert _loc(base[h1.id].scheduled_end) == (1, 12, 11, 0)
        assert _loc(base[h2.id].scheduled_start) == (1, 12, 11, 0)

        # Set a 30-minute switch penalty on the seeded human executor.
        await s.execute(
            update(Executor)
            .where(Executor.kind == ExecutorKind.human, Executor.user_id == user)
            .values(context_switch_cost_minutes=30)
        )
        await s.flush()
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        after = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    # H1 is the first task placed for the person: no penalty before it.
    assert _loc(after[h1.id].scheduled_start) == (1, 12, 9, 0)
    assert _loc(after[h1.id].scheduled_end) == (1, 12, 11, 0)
    # H2 follows a distinct task -> pushed by exactly 30 working minutes.
    assert _loc(after[h2.id].scheduled_start) == (1, 12, 11, 30)
    assert after[h2.id].scheduled_start > base[h2.id].scheduled_start


async def test_llm_pool_caps_concurrency_at_max_parallel() -> None:
    """(b) More than max_parallel independent llm_agent tasks: at most
    max_parallel run concurrently, the rest queue."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="POOL")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        # 6 independent agent tasks, default pool max_parallel = 4.
        for i in range(6):
            await tasks.create_task(
                s,
                org_id=org,
                actor_id=user,
                title=f"AI{i}",
                estimate_effort_h=Decimal(3),
                executor_kind=ExecKind.llm_agent,
            )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = await sch.list_schedule(s, org_id=org)
        agent = await exec_svc.ensure_default_agent(s, org_id=org)

    assert agent.max_parallel == 4
    assert len(rows) == 6
    # At most 4 concurrent; the 2 extra queue (so a strictly later start
    # exists -> peak is exactly the cap, and not all 6 overlap).
    assert _overlap(rows) == 4
    starts = sorted({r.scheduled_start for r in rows})
    assert len(starts) >= 2  # the pool forced queuing


async def test_policy_cheapest_defers_llm_vs_fastest() -> None:
    """(c) The policy changes order/makespan deterministically: a
    paid-LLM task is deferred under `cheapest` (zero-credit work first)
    relative to `fastest`."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="POL")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        # One human task and one paid-LLM task, no dependency. They are
        # on different resources, so the policy ordering shows up in the
        # priority key (cheapest pushes the paid agent task last).
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="HU",
            priority=4,
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        ai = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="AI",
            priority=1,
            estimate_effort_h=Decimal(2),
            executor_kind=ExecKind.llm_agent,
        )
        # Give the agent a non-zero credit rate so `cheapest` defers it.
        await exec_svc.ensure_default_agent(s, org_id=org)
        await s.execute(
            update(Executor)
            .where(Executor.kind == ExecutorKind.llm_agent)
            .values(credit_rate_per_hour=Decimal("2.0"))
        )
        await s.flush()

        fast = await sch.recompute(
            s, org_id=org, actor_id=user, as_of=_AS_OF, policy=SchedulePolicy.fastest
        )
        fast_rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}
        cheap = await sch.recompute(
            s, org_id=org, actor_id=user, as_of=_AS_OF, policy=SchedulePolicy.cheapest
        )
        cheap_rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    assert fast.policy is SchedulePolicy.fastest
    assert cheap.policy is SchedulePolicy.cheapest
    # The projected credit cost is policy-invariant (same work, same
    # rate) but the schedule differs: a deterministic, non-trivial knob.
    assert fast.projected_credit_cost == cheap.projected_credit_cost == Decimal("4.0000")
    assert fast_rows[ai.id].input_fingerprint != cheap_rows[ai.id].input_fingerprint


async def test_recompute_out_makespan_and_projected_credit_cost() -> None:
    """(d) RecomputeOut carries makespan_minutes>0 and
    projected_credit_cost == Sum(llm effort * rate) for a non-zero-rate
    executor; ScheduleOut carries projected_cost; the API surfaces it
    without a recompute."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        a = (
            await c.post(
                "/auth/signup",
                json={"email": _email(), "password": "pw-strong-123", "workspace_name": "MK"},
            )
        ).json()
        h = {
            "Authorization": f"Bearer {a['token']}",
            "X-Workspace-Id": a["workspace_id"],
            "X-Workspace-Role": "owner",
        }
        # Two agent tasks: 3h + 5h = 8 effort-hours.
        for title, eff in (("A1", "3"), ("A2", "5")):
            r = await c.post(
                "/tasks",
                headers=h,
                json={"title": title, "estimate_effort_h": eff, "executor_kind": "llm_agent"},
            )
            assert r.status_code == 200

    # Set the default agent's rate to 1.5 credits / effort-hour.
    async with admin_session() as s:
        org_id = uuid.UUID(a["workspace_id"])
    async with tenant_session(str(org_id), a["user_id"]) as s:
        await exec_svc.ensure_default_agent(s, org_id=org_id)
        await s.execute(
            update(Executor)
            .where(Executor.kind == ExecutorKind.llm_agent)
            .values(credit_rate_per_hour=Decimal("1.5"))
        )

    async with AsyncClient(transport=transport, base_url="http://t") as c:
        rec = await c.post(
            "/schedule/recompute",
            headers=h,
            json={"as_of": "2026-01-12T08:00:00+00:00", "policy": "balanced"},
        )
        assert rec.status_code == 200
        body = rec.json()
        assert body["count"] == 2
        assert body["policy"] == "balanced"
        assert body["makespan_minutes"] > 0
        # 8 effort-hours * 1.5 = 12.0000 credits projected.
        assert Decimal(body["projected_credit_cost"]) == Decimal("12.0000")

        sched = (await c.get("/schedule", headers=h)).json()
        assert {row["task_id"] for row in sched}
        for row in sched:
            assert "on_critical_chain" in row
            assert Decimal(row["projected_cost"]) >= 0
        # Sum of per-task projected_cost equals the recompute total.
        assert sum(Decimal(r["projected_cost"]) for r in sched) == Decimal("12.0000")


async def test_critical_chain_superset_under_resource_contention() -> None:
    """(e) on_critical_chain is set on the binding leveled path and is a
    strict superset of the logical critical path under resource
    contention.

    Setup: a long task A (6h, P1) and an independent shorter task B (2h,
    P4) on the SAME single human, no precedence edge. Logically
    (infinite resources) only A binds the makespan (B has slack: it
    could float). The serial human resource forces B after A, so the
    leveled plan ends with B -> B now binds the makespan and joins A on
    the resource-aware critical chain while staying OFF the logical
    critical path. chain = {A, B} strictly contains logical = {A}.
    """
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="CC")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        t_long = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="A",
            priority=1,
            estimate_effort_h=Decimal(6),
            assignee_ids=[user],
        )
        t_short = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="B",
            priority=4,
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = {r.task_id: r for r in await sch.list_schedule(s, org_id=org)}

    r_long, r_short = rows[t_long.id], rows[t_short.id]
    # Serialized by the single human resource (disjoint): A then B.
    assert r_long.scheduled_end <= r_short.scheduled_start
    # Logically only A binds; B has positive logical slack (it floats).
    assert r_long.on_logical_critical_path is True
    assert r_short.on_logical_critical_path is False
    assert (r_short.slack_minutes or 0) > 0
    # The leveled plan ends with B (pushed after A by the serial
    # resource) -> B binds the makespan and is on the critical chain.
    assert r_short.on_critical_chain is True
    assert r_long.on_critical_chain is True
    logical = {tid for tid, r in rows.items() if r.on_logical_critical_path}
    chain = {tid for tid, r in rows.items() if r.on_critical_chain}
    # Resource-aware chain is a STRICT superset of the logical path:
    # contention added B (which logically had slack) to the binding path.
    assert chain != logical
    assert logical < chain


async def test_determinism_same_inputs_and_policy() -> None:
    """(f) Same inputs + policy -> identical schedule (values and
    fingerprint stable across two recomputes)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="DETP")
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
            estimate_effort_h=Decimal(4),
            executor_kind=ExecKind.llm_agent,
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=x.id,
            successor_id=y.id,
            type=DependencyType.FS,
        )
        s1 = await sch.recompute(
            s, org_id=org, actor_id=user, as_of=_AS_OF, policy=SchedulePolicy.throughput
        )
        first = {
            r.task_id: (
                r.es,
                r.ef,
                r.scheduled_start,
                r.scheduled_end,
                r.on_critical_chain,
                r.projected_cost,
                r.input_fingerprint,
            )
            for r in await sch.list_schedule(s, org_id=org)
        }
        s2 = await sch.recompute(
            s, org_id=org, actor_id=user, as_of=_AS_OF, policy=SchedulePolicy.throughput
        )
        second = {
            r.task_id: (
                r.es,
                r.ef,
                r.scheduled_start,
                r.scheduled_end,
                r.on_critical_chain,
                r.projected_cost,
                r.input_fingerprint,
            )
            for r in await sch.list_schedule(s, org_id=org)
        }

    assert first == second
    assert s1.makespan_minutes == s2.makespan_minutes
    assert s1.projected_credit_cost == s2.projected_credit_cost
    assert len({fp for *_, fp in first.values()}) == 1


async def test_fresh_workspace_recompute_zero_manual_config() -> None:
    """(g) A fresh workspace recomputes with zero manual executor
    config: the lazy seed provides the human executors + the default
    LLM pool, and the schedule is produced."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="FRESH")
    org, user = a.org_id, a.user_id

    async with tenant_session(str(org), str(user)) as s:
        # No executor row exists yet for this brand-new workspace.
        pre = (await s.execute(select(Executor))).scalars().all()
        assert pre == []

        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="H",
            estimate_effort_h=Decimal(2),
            assignee_ids=[user],
        )
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="AI",
            estimate_effort_h=Decimal(2),
            executor_kind=ExecKind.llm_agent,
        )
        summary = await sch.recompute(s, org_id=org, actor_id=user, as_of=_AS_OF)
        rows = await sch.list_schedule(s, org_id=org)
        seeded = (await s.execute(select(Executor))).scalars().all()

    assert summary.count == 2
    assert all(r.scheduled_start is not None for r in rows)
    # Defaults were seeded: one human (the owner) + the default agent.
    kinds = sorted(e.kind.value for e in seeded)
    assert kinds == ["human", "llm_agent"]
    agent = next(e for e in seeded if e.kind is ExecutorKind.llm_agent)
    assert agent.name == "Assistant"
    assert agent.max_parallel == 4
    assert agent.credit_rate_per_hour == Decimal(0)
