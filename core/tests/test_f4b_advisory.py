"""F4b advisory + budgets (DB-backed), FR-13/FR-14 verification.

Deterministic feasibility + ranking for what-can-i-do-now (effort fit,
dependency block, event overlap, location/context filters), errands
aggregation, budget knapsack (must-have first, value-density fill,
explicit exclusions, determinism), budget consumption, cross-org
isolation, and multi-project-within-org (not a memory breach).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from flow_core.db import admin_session, tenant_session
from flow_core.models.budget import BudgetPeriod
from flow_core.models.dependency import DependencyType
from flow_core.models.tag import TagKind
from flow_core.models.task import Necessity
from flow_core.services import actors as actors_svc
from flow_core.services import advisory, budgets, tasks, taxonomy
from flow_core.services import dependencies as deps
from flow_core.services import identities as identities_svc
from flow_core.services.auth import signup

_WIN = dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_what_can_i_do_now_feasibility_and_ranking() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ADV")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        ctx_tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="ctx:computer"
        )
        common = dict(org_id=org, actor_id=user, assignee_ids=[user])
        ta = await tasks.create_task(
            s,
            title="A-must",
            priority=2,
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.must,
            **common,
        )
        tb = await tasks.create_task(
            s,
            title="B-should-p1",
            priority=1,
            estimate_effort_h=Decimal(1),
            necessity=Necessity.should,
            **common,
        )
        await tasks.create_task(
            s,
            title="C-too-big",
            priority=1,
            estimate_effort_h=Decimal(4),
            necessity=Necessity.should,
            **common,
        )
        td = await tasks.create_task(
            s,
            title="D-needs-ctx",
            priority=1,
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.should,
            tag_ids=[ctx_tag.id],
            **common,
        )
        te = await tasks.create_task(
            s,
            title="E-office",
            priority=3,
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.should,
            location="office",
            **common,
        )

        # 60-min window, no location/context: A,B,E feasible (C too big,
        # D needs ctx:computer). Order: must, then should by priority.
        r = await advisory.what_can_i_do_now(
            s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60
        )
        assert [x.task_id for x in r] == [ta.id, tb.id, te.id]
        assert r[0].necessity is Necessity.must

        # Providing the context unlocks D.
        r2 = await advisory.what_can_i_do_now(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            context_tags=["ctx:computer"],
        )
        assert td.id in {x.task_id for x in r2}

        # An overlapping appointment-task (migration 0094 + 0097) makes
        # the claimed free window not free. The advisory ``_user_busy``
        # check reads task_participants; the 0096 trigger mirrors the
        # assignee into a participant row so a single create suffices.
        await actors_svc.mint_user_handle(s, user_id=user, seed="adv")
        ident = await identities_svc.ensure_for_user(s, org_id=org, user_id=user)
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="Busy",
            assignee_id=ident.id,
            start_at=_WIN + dt.timedelta(minutes=30),
            duration_minutes=60,
        )
        assert (
            await advisory.what_can_i_do_now(
                s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60
            )
            == []
        )


async def test_what_now_dependency_block() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="BLK")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        pred = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="pred",
            estimate_effort_h=Decimal("0.5"),
            assignee_ids=[user],
        )
        succ = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="succ",
            estimate_effort_h=Decimal("0.5"),
            assignee_ids=[user],
        )
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=pred.id,
            successor_id=succ.id,
            type=DependencyType.FS,
        )
        ids = {
            x.task_id
            for x in await advisory.what_can_i_do_now(
                s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60
            )
        }
    assert pred.id in ids  # pred is free
    assert succ.id not in ids  # succ blocked by non-terminal pred


async def test_prioritize_within_budget_knapsack_and_determinism() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="BGT")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        b = await budgets.create_budget(
            s,
            org_id=org,
            actor_id=user,
            name="Home",
            period_kind=BudgetPeriod.month,
            period_start=dt.date(2026, 1, 1),
            period_end=dt.date(2026, 1, 31),
            amount=Decimal(100),
        )
        mk = dict(org_id=org, actor_id=user, budget_id=b.id)
        m1 = await tasks.create_task(
            s,
            title="m1",
            priority=2,
            monetary_cost=Decimal(40),
            necessity=Necessity.must,
            **mk,
        )
        m2 = await tasks.create_task(
            s,
            title="m2",
            priority=1,
            monetary_cost=Decimal(30),
            necessity=Necessity.must,
            **mk,
        )
        s1 = await tasks.create_task(
            s,
            title="s1",
            priority=1,
            monetary_cost=Decimal(50),
            necessity=Necessity.should,
            **mk,
        )
        s2 = await tasks.create_task(
            s,
            title="s2",
            priority=3,
            monetary_cost=Decimal(20),
            necessity=Necessity.should,
            **mk,
        )
        n1 = await tasks.create_task(
            s,
            title="n1",
            priority=3,
            monetary_cost=Decimal(10),
            necessity=Necessity.nice,
            **mk,
        )
        plan = await advisory.prioritize_within_budget(s, org_id=org, actor_id=user, budget_id=b.id)
        plan2 = await advisory.prioritize_within_budget(
            s, org_id=org, actor_id=user, budget_id=b.id
        )

    sel = {p.task_id for p in plan.selected}
    assert sel == {m1.id, m2.id, s2.id, n1.id}  # s1 priced out
    assert plan.allocated == Decimal(100)
    assert plan.residual == Decimal(0)
    assert {e["task_id"] for e in plan.excluded} == {str(s1.id)}
    assert plan.excluded[0]["reason"] == "budget_exhausted"
    # Deterministic: identical plan for identical input.
    assert [p.task_id for p in plan.selected] == [p.task_id for p in plan2.selected]


async def test_budget_consumption_excludes_inactive() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="CONS")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        b = await budgets.create_budget(
            s,
            org_id=org,
            actor_id=user,
            name="B",
            period_kind=BudgetPeriod.custom,
            period_start=dt.date(2026, 1, 1),
            period_end=dt.date(2026, 12, 31),
            amount=Decimal(200),
        )
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="c1",
            monetary_cost=Decimal(50),
            budget_id=b.id,
        )
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="c2",
            monetary_cost=Decimal(30),
            budget_id=b.id,
        )
        gone = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="c3",
            monetary_cost=Decimal(100),
            budget_id=b.id,
        )
        await tasks.archive_task(
            s,
            org_id=org,
            actor_id=user,
            task_id=gone.id,
            expected_version=gone.version,
        )
        c = await budgets.consumption(s, org_id=org, budget_id=b.id)
    assert c.consumed == Decimal(80)
    assert c.residual == Decimal(120)
    assert c.task_count == 2


async def test_advisory_multi_project_within_org_not_isolation_breach() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="MP")
        other = await signup(s, email=_email(), password="pw-strong-123", org_name="MP-OTHER")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        p1 = await taxonomy.create_project(s, org_id=org, actor_id=user, name="P1")
        p2 = await taxonomy.create_project(s, org_id=org, actor_id=user, name="P2")
        t1 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="brico-1",
            estimate_effort_h=Decimal("0.5"),
            location="brico",
            tag_ids=[p1.id],
            assignee_ids=[user],
        )
        t2 = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="brico-2",
            estimate_effort_h=Decimal("0.5"),
            location="brico",
            tag_ids=[p2.id],
            assignee_ids=[user],
        )
        items = await advisory.errands(s, org_id=org, actor_id=user, location="brico")
    ids = {i.task_id for i in items}
    # Advisory spans projects within the org (FR-13): both returned.
    assert ids == {t1.id, t2.id}

    # Cross-org: the other org sees none of it.
    async with tenant_session(str(other.org_id), str(other.user_id)) as s:
        assert (
            await advisory.errands(s, org_id=other.org_id, actor_id=other.user_id, location="brico")
            == []
        )
        assert await budgets.list_budgets(s, org_id=other.org_id) == []
