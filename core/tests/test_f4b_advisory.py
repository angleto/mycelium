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
from typing import Any

from sqlalchemy import select

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.models.budget import BudgetPeriod
from mycelium_core.models.dependency import DependencyType
from mycelium_core.models.tag import TagKind
from mycelium_core.models.task import Necessity
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import actors as actors_svc
from mycelium_core.services import advisory, budgets, tasks, taxonomy
from mycelium_core.services import dependencies as deps
from mycelium_core.services import identities as identities_svc
from mycelium_core.services.auth import signup

_WIN = dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def _fits(*args: Any, **kwargs: Any) -> list[advisory.FeasibleTask]:
    """The ``.fits`` partition of ``what_can_i_do_now`` -- the doable, ranked
    list most tests assert on. The ``over_window`` partition (effort exceeds
    the window) has dedicated coverage below."""
    return (await advisory.what_can_i_do_now(*args, **kwargs)).fits


async def test_what_can_i_do_now_excludes_terminal_state_tasks() -> None:
    """A task in a terminal workflow state must never be ranked: you cannot
    'do now' a finished task (user report 2026-06-03). Regression guard for
    the ``is_terminal.is_(False)`` filter in ``_owned_actionable``."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ADVT")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            assignee_ids=[user],
            title="finish-me",
            estimate_effort_h=Decimal("0.5"),
        )
        r = await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60)
        assert t.id in [x.task_id for x in r]

        terminal_id = (
            await s.execute(
                select(WorkflowState.id).where(WorkflowState.is_terminal.is_(True)).limit(1)
            )
        ).scalar_one()
        t.state_id = terminal_id
        await s.flush()

        r2 = await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60)
        assert t.id not in [x.task_id for x in r2]


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
            importance=1,
            urgency=2,
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.must,
            **common,
        )
        tb = await tasks.create_task(
            s,
            title="B-should-p1",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal(1),
            necessity=Necessity.should,
            **common,
        )
        await tasks.create_task(
            s,
            title="C-too-big",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal(4),
            necessity=Necessity.should,
            **common,
        )
        td = await tasks.create_task(
            s,
            title="D-needs-ctx",
            importance=1,
            urgency=1,
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.should,
            tag_ids=[ctx_tag.id],
            **common,
        )
        te = await tasks.create_task(
            s,
            title="E-office",
            importance=1,
            urgency=3,
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.should,
            location="office",
            **common,
        )

        # 60-min window, no location/context: A,B,E feasible (C too big,
        # D needs ctx:computer). Order: must, then should by priority.
        r = await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60)
        assert [x.task_id for x in r] == [ta.id, tb.id, te.id]
        assert r[0].necessity is Necessity.must

        # Providing the context unlocks D.
        r2 = await _fits(
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
            await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60) == []
        )


def _by_id(rows: list[advisory.FeasibleTask]) -> dict[uuid.UUID, advisory.FeasibleTask]:
    return {r.task_id: r for r in rows}


async def test_what_now_deadline_urgency_outranks_necessity() -> None:
    """T1: a soon-due could/low-priority task (at_risk / overdue) ranks
    ABOVE a comfortable must, because deadline urgency is first-class."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="URG")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        common = dict(org_id=org, actor_id=user, assignee_ids=[user])
        comfy_must = await tasks.create_task(
            s,
            title="comfortable-must",
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.must,
            due_date=_WIN + dt.timedelta(days=7),
            **common,
        )
        at_risk_could = await tasks.create_task(
            s,
            title="at-risk-could",
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.could,
            due_date=_WIN + dt.timedelta(minutes=20),  # 30min effort -> slack -10
            **common,
        )
        overdue_could = await tasks.create_task(
            s,
            title="overdue-could",
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.could,
            due_date=_WIN - dt.timedelta(days=1),  # date-in-the-past -> overdue
            **common,
        )
        r = await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60)
    ids = [x.task_id for x in r]
    # overdue (rank 0) then at_risk (rank 1) then comfortable must (rank 2).
    assert ids == [overdue_could.id, at_risk_could.id, comfy_must.id]
    by = _by_id(r)
    assert by[overdue_could.id].deadline_bucket == "overdue"
    assert by[at_risk_could.id].deadline_bucket == "at_risk"
    assert by[at_risk_could.id].slack_minutes == -10
    assert by[comfy_must.id].deadline_bucket == "comfortable"


async def test_what_now_deadline_bucket_boundaries() -> None:
    """T1: slack==0 -> tight, slack just below 0 -> at_risk, slack just
    below the window -> tight, slack==window -> comfortable."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="BND")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        common = dict(
            org_id=org, actor_id=user, assignee_ids=[user], estimate_effort_h=Decimal("0.5")
        )
        # 30-min effort, 60-min window. slack = (due-window)/60 - 30.
        at_risk = await tasks.create_task(
            s, title="b-atrisk", due_date=_WIN + dt.timedelta(minutes=29), **common
        )  # slack -1
        tight0 = await tasks.create_task(
            s, title="b-tight0", due_date=_WIN + dt.timedelta(minutes=30), **common
        )  # slack 0
        tight59 = await tasks.create_task(
            s, title="b-tight59", due_date=_WIN + dt.timedelta(minutes=89), **common
        )  # slack 59
        comfy = await tasks.create_task(
            s, title="b-comfy", due_date=_WIN + dt.timedelta(minutes=90), **common
        )  # slack 60 == window
        r = await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60)
    by = _by_id(r)
    assert (by[at_risk.id].slack_minutes, by[at_risk.id].deadline_bucket) == (-1, "at_risk")
    assert (by[tight0.id].slack_minutes, by[tight0.id].deadline_bucket) == (0, "tight")
    assert (by[tight59.id].slack_minutes, by[tight59.id].deadline_bucket) == (59, "tight")
    assert (by[comfy.id].slack_minutes, by[comfy.id].deadline_bucket) == (60, "comfortable")


async def test_what_now_no_due_date_bucket_none_sorts_after_tight() -> None:
    """T1: a no-due_date task has slack None / bucket 'none' and, within
    the same necessity+priority tie group, sorts AFTER a tight task
    (finite slack beats the +inf sentinel)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="NONE")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        common = dict(
            org_id=org,
            actor_id=user,
            assignee_ids=[user],
            estimate_effort_h=Decimal("0.5"),
            necessity=Necessity.should,
            importance=2,
            urgency=2,
        )
        tight = await tasks.create_task(
            s, title="n-tight", due_date=_WIN + dt.timedelta(minutes=40), **common
        )  # slack 10 -> tight
        nodue = await tasks.create_task(s, title="n-nodue", **common)
        r = await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60)
    ids = [x.task_id for x in r]
    by = _by_id(r)
    assert by[nodue.id].slack_minutes is None
    assert by[nodue.id].deadline_bucket == "none"
    assert ids.index(tight.id) < ids.index(nodue.id)


async def test_what_now_ranking_is_deterministic_incl_signal() -> None:
    """T1: identical inputs -> byte-identical list, slack/bucket included
    (FeasibleTask is a frozen dataclass with value equality)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="DET")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        common = dict(
            org_id=org, actor_id=user, assignee_ids=[user], estimate_effort_h=Decimal("0.5")
        )
        await tasks.create_task(s, title="d1", due_date=_WIN + dt.timedelta(minutes=20), **common)
        await tasks.create_task(s, title="d2", **common)
        await tasks.create_task(s, title="d3", due_date=_WIN + dt.timedelta(days=3), **common)
        r1 = await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60)
        r2 = await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60)
    assert r1 == r2


async def test_what_now_core_coerces_naive_window_start() -> None:
    """T1: a naive window_start is coerced to UTC in the core so the
    slack subtraction against the aware due_date never raises (latent
    500 guard); result matches the aware call."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="TZ")
    org, user = a.org_id, a.user_id
    naive = _WIN.replace(tzinfo=None)
    async with tenant_session(str(org), str(user)) as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            assignee_ids=[user],
            title="tz-task",
            estimate_effort_h=Decimal("0.5"),
            due_date=_WIN + dt.timedelta(minutes=20),
        )
        aware_r = await _fits(s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60)
        naive_r = await _fits(s, org_id=org, actor_id=user, window_start=naive, duration_minutes=60)
    assert naive_r == aware_r
    assert naive_r[0].slack_minutes == -10


async def test_what_now_focus_tag_selection_and_empty_inactive() -> None:
    """T2: focus_tag_ids keeps only tasks carrying a selected
    project/client tag; an EMPTY list is inactive (== None)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="FOCUS")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        pa = await taxonomy.create_project(s, org_id=org, actor_id=user, name="A")
        pb = await taxonomy.create_project(s, org_id=org, actor_id=user, name="B")
        common = dict(
            org_id=org, actor_id=user, assignee_ids=[user], estimate_effort_h=Decimal("0.5")
        )
        ta = await tasks.create_task(s, title="in-A", tag_ids=[pa.id], **common)
        tb = await tasks.create_task(s, title="in-B", tag_ids=[pb.id], **common)
        tc = await tasks.create_task(s, title="no-project", **common)
        only_a = await _fits(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            focus_tag_ids=[pa.id],
        )
        empty = await _fits(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            focus_tag_ids=[],
        )
        none = await _fits(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
        )
    assert {x.task_id for x in only_a} == {ta.id}
    # Empty list behaves exactly like the omitted (None) selector.
    assert {x.task_id for x in empty} == {ta.id, tb.id, tc.id}
    assert {x.task_id for x in none} == {ta.id, tb.id, tc.id}


async def test_what_now_min_priority_selector() -> None:
    """T2: min_priority is an importance FLOOR -- keep priority <= the level
    (priority = importance*urgency, 1=top..25), mirroring min_necessity."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="PRIO")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        common = dict(
            org_id=org, actor_id=user, assignee_ids=[user], estimate_effort_h=Decimal("0.5")
        )
        p4 = await tasks.create_task(s, title="p4", importance=2, urgency=2, **common)  # 4
        p9 = await tasks.create_task(s, title="p9", importance=3, urgency=3, **common)  # 9
        r = await _fits(
            s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60, min_priority=5
        )
    ids = {x.task_id for x in r}
    assert p4.id in ids
    assert p9.id not in ids


async def test_what_now_min_necessity_selector() -> None:
    """T2: min_necessity keeps necessities at/above the floor."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="NEC")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        common = dict(
            org_id=org, actor_id=user, assignee_ids=[user], estimate_effort_h=Decimal("0.5")
        )
        must = await tasks.create_task(s, title="m", necessity=Necessity.must, **common)
        should = await tasks.create_task(s, title="s", necessity=Necessity.should, **common)
        could = await tasks.create_task(s, title="c", necessity=Necessity.could, **common)
        r = await _fits(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            min_necessity=Necessity.should,
        )
    ids = {x.task_id for x in r}
    assert must.id in ids and should.id in ids
    assert could.id not in ids


async def test_what_now_focus_is_hard_scope_others_union() -> None:
    """Focus is a HARD scope (AND): a task must carry a focus tag. The other
    selectors then UNION *within* that scope -- focus_tag_ids=[A] alone
    keeps every A task; adding min_priority=5 keeps only the A tasks that
    are also priority<=5, while a priority<=5 task OUTSIDE A still drops."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="SCOPE")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        pa = await taxonomy.create_project(s, org_id=org, actor_id=user, name="A")
        common = dict(
            org_id=org, actor_id=user, assignee_ids=[user], estimate_effort_h=Decimal("0.5")
        )
        a_p4 = await tasks.create_task(
            s, title="A-p4", tag_ids=[pa.id], importance=2, urgency=2, **common
        )  # in A, priority 4
        a_p9 = await tasks.create_task(
            s, title="A-p9", tag_ids=[pa.id], importance=3, urgency=3, **common
        )  # in A, priority 9
        cheap_noA = await tasks.create_task(
            s, title="noA-p4", importance=2, urgency=2, **common
        )  # not in A, priority 4
        scoped = await _fits(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            focus_tag_ids=[pa.id],
        )
        scoped_prio = await _fits(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            focus_tag_ids=[pa.id],
            min_priority=5,
        )
    # Focus alone scopes to A regardless of priority.
    assert {x.task_id for x in scoped} == {a_p4.id, a_p9.id}
    # Within the focus, min_priority narrows; the cheap task outside A never
    # re-enters (focus is AND, not part of the OR).
    assert {x.task_id for x in scoped_prio} == {a_p4.id}
    assert cheap_noA.id not in {x.task_id for x in scoped_prio}


async def test_what_now_location_soft_substring_match() -> None:
    """location is a SOFT, case-insensitive substring place filter: a task
    bound to a matching place stays (a fragment finds the full string), a
    task bound to a DIFFERENT place drops, and a task with no location stays
    (doable anywhere)."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="LOC")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        common = dict(
            org_id=org, actor_id=user, assignee_ids=[user], estimate_effort_h=Decimal("0.5")
        )
        here = await tasks.create_task(
            s, title="at-camp", location="Santo Stefano Quisquina (camp)", **common
        )
        elsewhere = await tasks.create_task(s, title="at-office", location="Office", **common)
        anywhere = await tasks.create_task(s, title="anywhere", **common)
        r = await _fits(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            location="stefano",
        )
    ids = {x.task_id for x in r}
    assert here.id in ids  # fragment + case-insensitive substring match
    assert anywhere.id in ids  # no location -> doable anywhere -> kept
    assert elsewhere.id not in ids  # bound to a different place -> dropped


async def test_what_now_any_tag_selector_distinct_from_ctx_gate() -> None:
    """T2: any_tag_ids is a generic-tag SELECTION (only carriers), it is
    NOT the ctx: capability GATE; a ctx-only task is not selected by it."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ANYTAG")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        g = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="energy"
        )
        cx = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="ctx:computer"
        )
        common = dict(
            org_id=org, actor_id=user, assignee_ids=[user], estimate_effort_h=Decimal("0.5")
        )
        t_g = await tasks.create_task(s, title="has-energy", tag_ids=[g.id], **common)
        await tasks.create_task(s, title="ctx-only", tag_ids=[cx.id], **common)
        await tasks.create_task(s, title="plain", **common)
        # ctx provided so the ctx-only task is not gated out; even so it is
        # not SELECTED, because it does not carry the selected generic tag.
        r = await _fits(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            any_tag_ids=[g.id],
            context_tags=["ctx:computer"],
        )
    assert {x.task_id for x in r} == {t_g.id}


async def test_what_now_foreign_focus_tag_selects_nothing() -> None:
    """T2 RLS: a focus tag id from another org matches nothing (no leak,
    no error), because the tenant-session join never sees it."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="RLS-A")
        b = await signup(s, email=_email(), password="pw-strong-123", org_name="RLS-B")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(b.org_id), str(b.user_id)) as s:
        foreign = await taxonomy.create_project(s, org_id=b.org_id, actor_id=b.user_id, name="X")
    async with tenant_session(str(org), str(user)) as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            assignee_ids=[user],
            title="mine",
            estimate_effort_h=Decimal("0.5"),
        )
        r = await _fits(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            focus_tag_ids=[foreign.id],
        )
    assert r == []


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
            for x in await _fits(
                s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60
            )
        }
    assert pred.id in ids  # pred is free
    assert succ.id not in ids  # succ blocked by non-terminal pred


async def test_what_now_over_window_partition() -> None:
    """A task that clears every filter but needs MORE time than the window is
    not dropped: it lands in ``over_window`` (not ``fits``), carrying its full
    effort as remaining_minutes, while a fitting task stays in ``fits``."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="OVW")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        common = dict(org_id=org, actor_id=user, assignee_ids=[user])
        fits = await tasks.create_task(
            s, title="fits-30", estimate_effort_h=Decimal("0.5"), **common
        )
        too_long = await tasks.create_task(
            s, title="needs-90", estimate_effort_h=Decimal("1.5"), **common
        )
        plan = await advisory.what_can_i_do_now(
            s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60
        )
    fit_ids = [x.task_id for x in plan.fits]
    over_ids = [x.task_id for x in plan.over_window]
    assert fits.id in fit_ids and fits.id not in over_ids
    assert too_long.id in over_ids and too_long.id not in fit_ids
    over = {x.task_id: x for x in plan.over_window}[too_long.id]
    assert over.remaining_minutes == 90  # full effort, even though the window is 60


async def test_what_now_over_window_keeps_overdue_must_visible() -> None:
    """Regression (user report 2026-06-17): a ``must`` whose effort exceeds the
    window must NOT silently vanish. An overdue, too-long must lands in
    ``over_window`` with its urgency bucket intact instead of disappearing."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="OVWMUST")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        training = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            assignee_ids=[user],
            title="climbing-75",
            estimate_effort_h=Decimal("1.25"),  # 75 min > 60-min window
            necessity=Necessity.must,
            due_date=_WIN - dt.timedelta(hours=2),  # already overdue
        )
        plan = await advisory.what_can_i_do_now(
            s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60
        )
    assert training.id not in [x.task_id for x in plan.fits]
    over = {x.task_id: x for x in plan.over_window}
    assert training.id in over
    assert over[training.id].deadline_bucket == "overdue"
    assert over[training.id].remaining_minutes == 75


async def test_what_now_over_window_still_obeys_hard_filters() -> None:
    """``over_window`` relaxes ONLY the time-fit: place, capability (ctx),
    dependency-block and focus scope still exclude a too-long task entirely
    (it appears in NEITHER partition). Only the unblocked, place-less,
    ctx-free task survives -- in ``over_window`` because it is too long."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="OVWHARD")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        ctx_tag = await taxonomy.create_tag(
            s, org_id=org, actor_id=user, kind=TagKind.generic, name="ctx:computer"
        )
        # 90-min effort everywhere -> every candidate is over the 60-min window.
        common = dict(
            org_id=org, actor_id=user, assignee_ids=[user], estimate_effort_h=Decimal("1.5")
        )
        wrong_place = await tasks.create_task(s, title="big-elsewhere", location="Office", **common)
        needs_ctx = await tasks.create_task(s, title="big-ctx", tag_ids=[ctx_tag.id], **common)
        pred = await tasks.create_task(s, title="big-pred", **common)
        succ = await tasks.create_task(s, title="big-succ", **common)
        await deps.add_dependency(
            s,
            org_id=org,
            actor_id=user,
            predecessor_id=pred.id,
            successor_id=succ.id,
            type=DependencyType.FS,
        )
        plan = await advisory.what_can_i_do_now(
            s,
            org_id=org,
            actor_id=user,
            window_start=_WIN,
            duration_minutes=60,
            location="camp",  # substring matches none of the placed tasks
        )
    everywhere = {x.task_id for x in plan.fits} | {x.task_id for x in plan.over_window}
    assert wrong_place.id not in everywhere  # bound to a different place
    assert needs_ctx.id not in everywhere  # ctx:computer not provided
    assert succ.id not in everywhere  # blocked by the non-terminal pred
    assert pred.id in {x.task_id for x in plan.over_window}  # survivor, just too long


async def test_what_now_busy_window_empties_both_partitions() -> None:
    """A non-free window yields an EMPTY plan in BOTH partitions: if you are
    busy now there is nothing to start, over-window candidates included."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="OVWBUSY")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            assignee_ids=[user],
            title="big-task",
            estimate_effort_h=Decimal("1.5"),  # would be an over_window candidate
        )
        await actors_svc.mint_user_handle(s, user_id=user, seed="busy")
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
        plan = await advisory.what_can_i_do_now(
            s, org_id=org, actor_id=user, window_start=_WIN, duration_minutes=60
        )
    assert plan.fits == []
    assert plan.over_window == []


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
            importance=1,
            urgency=2,
            monetary_cost=Decimal(40),
            necessity=Necessity.must,
            **mk,
        )
        m2 = await tasks.create_task(
            s,
            title="m2",
            importance=1,
            urgency=1,
            monetary_cost=Decimal(30),
            necessity=Necessity.must,
            **mk,
        )
        s1 = await tasks.create_task(
            s,
            title="s1",
            importance=1,
            urgency=1,
            monetary_cost=Decimal(50),
            necessity=Necessity.should,
            **mk,
        )
        s2 = await tasks.create_task(
            s,
            title="s2",
            importance=1,
            urgency=3,
            monetary_cost=Decimal(20),
            necessity=Necessity.should,
            **mk,
        )
        n1 = await tasks.create_task(
            s,
            title="n1",
            importance=1,
            urgency=3,
            monetary_cost=Decimal(10),
            necessity=Necessity.could,
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
