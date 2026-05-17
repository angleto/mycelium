"""Planning advisory layer (docs/adr/0013, FR-13).

Deterministic and explainable decision core; the LLM/MCP is only the
natural-language frontend (it translates the request and narrates the
result, it does not decide). Three archetypes:

- ``what_can_i_do_now``: feasible tasks for a free window;
- ``errands``: tasks relevant to a place/context across the org;
- ``prioritize_within_budget``: a constrained priority/value-density
  selection (must-have first) within a budget envelope.

Same input -> same output. This operates on the user's tasks within an
org (possibly multi-project); that is NOT a memory-isolation breach
(ADR-0007 governs RAG/email content, not the user's task list).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.budget import Budget
from flow_core.models.event import Event, EventParticipant
from flow_core.models.membership import Role
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import ExecKind, Necessity, Task
from flow_core.models.task_assignee import TaskAssignee
from flow_core.models.task_tag import TaskTag
from flow_core.models.workflow import WorkflowState
from flow_core.services.budgets import get_budget
from flow_core.services.dependencies import blocked_task_ids
from flow_core.services.rbac import require_role

_NEC_RANK: dict[Necessity, int] = {
    Necessity.must: 0,
    Necessity.should: 1,
    Necessity.nice: 2,
}
_NEC_WEIGHT: dict[Necessity, int] = {
    Necessity.must: 100,
    Necessity.should: 10,
    Necessity.nice: 3,
}
_CTX_PREFIX = "ctx:"
_PLACE_PREFIX = "place:"


@dataclass(frozen=True)
class FeasibleTask:
    task_id: uuid.UUID
    title: str
    necessity: Necessity
    priority: int
    due_date: dt.date | None
    remaining_minutes: int


@dataclass(frozen=True)
class ErrandItem:
    task_id: uuid.UUID
    title: str
    location: str | None
    necessity: Necessity
    priority: int


@dataclass(frozen=True)
class BudgetPick:
    task_id: uuid.UUID
    title: str
    cost: Decimal
    necessity: Necessity
    priority: int
    value: int


@dataclass(frozen=True)
class BudgetPlan:
    budget_id: uuid.UUID
    amount: Decimal
    currency: str
    allocated: Decimal
    residual: Decimal
    selected: list[BudgetPick]
    excluded: list[dict[str, str]]


def _effort_minutes(task: Task) -> int | None:
    h = task.remaining_effort_h if task.remaining_effort_h is not None else task.estimate_effort_h
    if h is None:
        return None
    return round(float(h) * 60)


async def _owned_actionable(
    session: AsyncSession, actor_id: uuid.UUID, *, human_only: bool
) -> list[Task]:
    """Active, non-terminal tasks the user owns (assignee or executor)."""
    assignee_ids = (
        (
            await session.execute(
                select(TaskAssignee.task_id).where(TaskAssignee.user_id == actor_id)
            )
        )
        .scalars()
        .all()
    )
    stmt = (
        select(Task)
        .join(WorkflowState, WorkflowState.id == Task.state_id)
        .where(
            Task.deleted_at.is_(None),
            Task.is_archived.is_(False),
            WorkflowState.is_terminal.is_(False),
            or_(
                Task.id.in_(assignee_ids),
                Task.executor_user_id == actor_id,
            ),
        )
    )
    if human_only:
        stmt = stmt.where(Task.executor_kind == ExecKind.human)
    return list((await session.execute(stmt)).scalars().unique().all())


async def _generic_tag_names(
    session: AsyncSession, task_ids: list[uuid.UUID]
) -> dict[uuid.UUID, set[str]]:
    if not task_ids:
        return {}
    rows = (
        await session.execute(
            select(TaskTag.task_id, Tag.name)
            .join(Tag, Tag.id == TaskTag.tag_id)
            .where(TaskTag.task_id.in_(task_ids), Tag.kind == TagKind.generic)
        )
    ).all()
    out: dict[uuid.UUID, set[str]] = {}
    for task_id, name in rows:
        out.setdefault(task_id, set()).add(name)
    return out


async def _user_busy(
    session: AsyncSession,
    user_id: uuid.UUID,
    start_at: dt.datetime,
    end_at: dt.datetime,
) -> bool:
    return (
        await session.execute(
            select(EventParticipant.user_id)
            .join(Event, Event.id == EventParticipant.event_id)
            .where(
                EventParticipant.user_id == user_id,
                Event.start_at < end_at,
                Event.end_at > start_at,
            )
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def what_can_i_do_now(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    window_start: dt.datetime,
    duration_minutes: int,
    location: str | None = None,
    context_tags: list[str] | None = None,
) -> list[FeasibleTask]:
    await require_role(session, org_id, actor_id, Role.member)
    if duration_minutes <= 0:
        return []
    window_end = window_start + dt.timedelta(minutes=duration_minutes)
    # The claimed free window must really be free (no-ubiquity, ADR-0008).
    if await _user_busy(session, actor_id, window_start, window_end):
        return []
    tasks = await _owned_actionable(session, actor_id, human_only=True)
    if not tasks:
        return []
    blocked = await blocked_task_ids(session, org_id=org_id, node_ids={t.id for t in tasks})
    tags = await _generic_tag_names(session, [t.id for t in tasks])
    have_ctx = {c for c in (context_tags or [])}
    out: list[FeasibleTask] = []
    for t in tasks:
        if t.id in blocked:
            continue
        minutes = _effort_minutes(t)
        if minutes is None or minutes <= 0 or minutes > duration_minutes:
            continue
        if location is not None and t.location is not None and t.location != location:
            continue
        required_ctx = {n for n in tags.get(t.id, set()) if n.startswith(_CTX_PREFIX)}
        if not required_ctx.issubset(have_ctx):
            continue
        out.append(
            FeasibleTask(
                task_id=t.id,
                title=t.title,
                necessity=t.necessity,
                priority=t.priority,
                due_date=t.due_date,
                remaining_minutes=minutes,
            )
        )
    out.sort(
        key=lambda f: (
            _NEC_RANK[f.necessity],
            f.priority,
            f.due_date or dt.date.max,
            f.remaining_minutes,
            str(f.task_id),
        )
    )
    return out


async def errands(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    location: str | None = None,
    context: str | None = None,
) -> list[ErrandItem]:
    await require_role(session, org_id, actor_id, Role.member)
    if location is None and context is None:
        return []
    stmt = (
        select(Task)
        .join(WorkflowState, WorkflowState.id == Task.state_id)
        .where(
            Task.deleted_at.is_(None),
            Task.is_archived.is_(False),
            WorkflowState.is_terminal.is_(False),
        )
    )
    tasks = list((await session.execute(stmt)).scalars().unique().all())
    tags = await _generic_tag_names(session, [t.id for t in tasks])
    out: list[ErrandItem] = []
    for t in tasks:
        names = tags.get(t.id, set())
        ok_loc = location is not None and (
            t.location == location or f"{_PLACE_PREFIX}{location}" in names
        )
        ok_ctx = context is not None and (
            context in names or any(n.startswith(context) for n in names)
        )
        if location is not None and context is not None:
            match = ok_loc and ok_ctx
        else:
            match = ok_loc or ok_ctx
        if not match:
            continue
        out.append(
            ErrandItem(
                task_id=t.id,
                title=t.title,
                location=t.location,
                necessity=t.necessity,
                priority=t.priority,
            )
        )
    out.sort(
        key=lambda e: (
            _NEC_RANK[e.necessity],
            e.priority,
            e.title,
            str(e.task_id),
        )
    )
    return out


def _value(task: Task) -> int:
    return _NEC_WEIGHT[task.necessity] * (5 - task.priority)


async def prioritize_within_budget(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    budget_id: uuid.UUID,
) -> BudgetPlan:
    """Deterministic constrained selection (ADR-0014): must-have first
    in priority order, then a value-density greedy fill of the residual.
    Same input -> same plan, with an explicit excluded list."""
    await require_role(session, org_id, actor_id, Role.member)
    budget: Budget = await get_budget(session, org_id=org_id, budget_id=budget_id)
    tasks = list(
        (
            await session.execute(
                select(Task)
                .join(WorkflowState, WorkflowState.id == Task.state_id)
                .where(
                    Task.budget_id == budget_id,
                    Task.deleted_at.is_(None),
                    Task.is_archived.is_(False),
                    WorkflowState.is_terminal.is_(False),
                    Task.monetary_cost.is_not(None),
                )
            )
        )
        .scalars()
        .unique()
        .all()
    )
    musts = [t for t in tasks if t.necessity is Necessity.must]
    optional = [t for t in tasks if t.necessity is not Necessity.must]
    musts.sort(key=lambda t: (t.priority, t.monetary_cost or Decimal(0), str(t.id)))

    def density(t: Task) -> Decimal:
        cost = t.monetary_cost or Decimal(0)
        if cost <= 0:
            return Decimal(10**12)
        return Decimal(_value(t)) / cost

    optional.sort(
        key=lambda t: (
            -density(t),
            _NEC_RANK[t.necessity],
            t.priority,
            t.monetary_cost or Decimal(0),
            str(t.id),
        )
    )

    allocated = Decimal(0)
    selected: list[BudgetPick] = []
    excluded: list[dict[str, str]] = []

    def pick(t: Task) -> None:
        nonlocal allocated
        cost = t.monetary_cost or Decimal(0)
        allocated += cost
        selected.append(
            BudgetPick(
                task_id=t.id,
                title=t.title,
                cost=cost,
                necessity=t.necessity,
                priority=t.priority,
                value=_value(t),
            )
        )

    for t in musts:
        cost = t.monetary_cost or Decimal(0)
        if allocated + cost <= budget.amount:
            pick(t)
        else:
            excluded.append({"task_id": str(t.id), "reason": "must_over_budget"})
    for t in optional:
        cost = t.monetary_cost or Decimal(0)
        if allocated + cost <= budget.amount:
            pick(t)
        else:
            excluded.append({"task_id": str(t.id), "reason": "budget_exhausted"})

    return BudgetPlan(
        budget_id=budget_id,
        amount=budget.amount,
        currency=budget.currency,
        allocated=allocated,
        residual=budget.amount - allocated,
        selected=selected,
        excluded=excluded,
    )
