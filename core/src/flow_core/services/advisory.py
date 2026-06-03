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

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.models.budget import Budget
from flow_core.models.identity import Identity, IdentityKind
from flow_core.models.membership import Role
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import Necessity, Task
from flow_core.models.task_collaborator import TaskCollaborator
from flow_core.models.task_tag import TaskTag
from flow_core.models.workflow import WorkflowState
from flow_core.services.budgets import get_budget
from flow_core.services.dependencies import blocked_task_ids
from flow_core.services.rbac import require_role

_NEC_RANK: dict[Necessity, int] = {
    Necessity.must: 0,
    Necessity.should: 1,
    Necessity.could: 2,
}
_NEC_WEIGHT: dict[Necessity, int] = {
    Necessity.must: 100,
    Necessity.should: 10,
    Necessity.could: 3,
}
_CTX_PREFIX = "ctx:"
_PLACE_PREFIX = "place:"

# Deadline urgency buckets (ADR-0013: deterministic, explainable). Slack
# is the spare minutes left before the deadline once this task is done,
# computed solely from the passed-in ``window_start`` (never now()).
_BUCKET_OVERDUE = "overdue"  # due_date already in the past
_BUCKET_AT_RISK = "at_risk"  # slack below the at-risk threshold
_BUCKET_TIGHT = "tight"  # 0 <= slack < the available window
_BUCKET_COMFORTABLE = "comfortable"  # slack >= the available window
_BUCKET_NONE = "none"  # no due_date at all

# Tunable (open product decision #2): a task is "at risk" once its slack
# drops below this many minutes. 0 = the window leaves no spare time
# before the deadline. Raw ``slack_minutes`` stays visible regardless.
_AT_RISK_THRESHOLD_MIN = 0

# Urgency-first: overdue and at_risk lift ABOVE necessity/priority; the
# remaining buckets keep the existing nec/priority/due ordering.
_BUCKET_RANK: dict[str, int] = {
    _BUCKET_OVERDUE: 0,
    _BUCKET_AT_RISK: 1,
    _BUCKET_TIGHT: 2,
    _BUCKET_COMFORTABLE: 2,
    _BUCKET_NONE: 2,
}
# Buckets whose raw slack carries ordering meaning inside their rank;
# the rest fall back to a +inf sentinel so the sort key stays a TOTAL
# order (mirrors the old _DT_MAX no-due-date sentinel).
_SLACK_ORDERED = frozenset({_BUCKET_OVERDUE, _BUCKET_AT_RISK, _BUCKET_TIGHT})
_SLACK_SENTINEL = float("inf")


def _deadline_signal(
    due_date: dt.datetime | None,
    window_start: dt.datetime,
    remaining_minutes: int,
    duration_minutes: int,
) -> tuple[int | None, str]:
    """``(slack_minutes, deadline_bucket)`` for one task.

    Pure: derives urgency only from the passed-in ``window_start`` (NEVER
    ``datetime.now()`` inside the core, ADR-0013), so identical inputs
    yield identical output and the ranking stays reproducible. ``due_date``
    is a timestamptz (aware); the caller guarantees ``window_start`` is
    aware too, so the subtraction never raises.
    """
    if due_date is None:
        return None, _BUCKET_NONE
    slack = round((due_date - window_start).total_seconds() / 60) - remaining_minutes
    if due_date < window_start:
        return slack, _BUCKET_OVERDUE
    if slack < _AT_RISK_THRESHOLD_MIN:
        return slack, _BUCKET_AT_RISK
    if slack < duration_minutes:
        return slack, _BUCKET_TIGHT
    return slack, _BUCKET_COMFORTABLE


@dataclass(frozen=True)
class FeasibleTask:
    task_id: uuid.UUID
    title: str
    necessity: Necessity
    priority: int
    # Migration 0005: deadline is a timestamptz (carries time-of-day).
    due_date: dt.datetime | None
    remaining_minutes: int
    # Deterministic deadline signal (ADR-0013): spare minutes before the
    # deadline once this task is done (None when no due_date) and its
    # urgency bucket. Returned so the SPA can show WHY a task ranks where
    # it does and the LLM narrator gets facts, not ranking authority.
    slack_minutes: int | None
    deadline_bucket: str


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
                select(TaskCollaborator.task_id).where(TaskCollaborator.user_id == actor_id)
            )
        )
        .scalars()
        .all()
    )
    stmt = (
        select(Task)
        .join(WorkflowState, WorkflowState.id == Task.state_id)
        # docs/adr/0028: ``executor_user_id`` is gone. The actor's
        # "my tasks" view joins through identities (the actor's user
        # identity in the current org) plus the legacy M:N assignees.
        .outerjoin(Identity, Identity.id == Task.assignee_id)
        .where(
            Task.deleted_at.is_(None),
            Task.is_archived.is_(False),
            WorkflowState.is_terminal.is_(False),
            or_(
                Task.id.in_(assignee_ids),
                and_(Identity.user_id == actor_id, Identity.kind == IdentityKind.user),
            ),
        )
    )
    if human_only:
        stmt = stmt.where(
            or_(
                Task.assignee_id.is_(None),
                and_(Identity.kind == IdentityKind.user),
            )
        )
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


async def _tag_ids_for_kinds(
    session: AsyncSession, task_ids: list[uuid.UUID], kinds: set[TagKind]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Per-task tag ids restricted to ``kinds``, for the selection filters
    (focus = project/client ids; any_tag = generic ids). Runs under the
    tenant session, so cross-org tag ids simply never appear (RLS)."""
    if not task_ids:
        return {}
    rows = (
        await session.execute(
            select(TaskTag.task_id, TaskTag.tag_id)
            .join(Tag, Tag.id == TaskTag.tag_id)
            .where(TaskTag.task_id.in_(task_ids), Tag.kind.in_(kinds))
        )
    ).all()
    out: dict[uuid.UUID, set[uuid.UUID]] = {}
    for task_id, tag_id in rows:
        out.setdefault(task_id, set()).add(tag_id)
    return out


async def _user_busy(
    session: AsyncSession,
    user_id: uuid.UUID,
    start_at: dt.datetime,
    end_at: dt.datetime,
) -> bool:
    # Migration 0094/0095/0097: appointment-tasks live on ``tasks`` and
    # additional participants live on ``task_participants``. An identity
    # is busy iff it has a participant row whose window overlaps. The
    # 0096 assignee-mirror trigger means the assignee shows up here too,
    # so a single participant query covers both axes.
    from sqlalchemy import func

    from flow_core.models.identity import Identity
    from flow_core.models.task import Task
    from flow_core.models.task_participant import TaskParticipant

    end_expr = func.tasks_event_end(TaskParticipant.start_at, TaskParticipant.duration_minutes)
    return (
        await session.execute(
            select(TaskParticipant.identity_id)
            .join(Identity, Identity.id == TaskParticipant.identity_id)
            .join(Task, Task.id == TaskParticipant.task_id)
            .where(
                Identity.user_id == user_id,
                TaskParticipant.start_at < end_at,
                end_expr > start_at,
                Task.is_archived.is_(False),
                Task.deleted_at.is_(None),
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
    focus_tag_ids: list[uuid.UUID] | None = None,
    any_tag_ids: list[uuid.UUID] | None = None,
    max_priority: int | None = None,
    min_necessity: Necessity | None = None,
) -> list[FeasibleTask]:
    """Feasible tasks for a free window, urgency-first ranked (ADR-0013).

    Optional SELECTION filters (requirement #3, "by priority OR focus OR
    tags") narrow within the actor's owned/feasible set, never widening:
    ``focus_tag_ids`` (project/client-kind tag ids, = SPA focus),
    ``any_tag_ids`` (generic-tag selection, distinct from the ``ctx:``
    capability GATE), ``max_priority`` (keep priority <= ceiling) and
    ``min_necessity`` (keep at/above the necessity floor). Semantics:
    UNION across whichever selectors are SET (a task is kept if it
    matches AT LEAST ONE), then the feasibility filters still apply on
    top. An EMPTY list means the selector is INACTIVE (identical to
    None) -- never "match nothing". No selector set => unchanged
    behaviour. All tag joins run under the tenant session (RLS), so a
    cross-org tag id selects nothing without error.
    """
    await require_role(session, org_id, actor_id, Role.member)
    if duration_minutes <= 0:
        return []
    # tz-aware guard (latent 500): due_date is timestamptz (aware); a
    # naive window_start would make the slack subtraction raise. The
    # edges (T4 router, T6 MCP) coerce, but defend here too so the core
    # never 500s and the documented contract (aware == UTC) holds.
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=dt.UTC)
    window_end = window_start + dt.timedelta(minutes=duration_minutes)
    # The claimed free window must really be free (no-ubiquity, ADR-0008).
    if await _user_busy(session, actor_id, window_start, window_end):
        return []
    tasks = await _owned_actionable(session, actor_id, human_only=True)
    if not tasks:
        return []
    task_ids = [t.id for t in tasks]
    blocked = await blocked_task_ids(session, org_id=org_id, node_ids=set(task_ids))
    tags = await _generic_tag_names(session, task_ids)
    have_ctx = {c for c in (context_tags or [])}
    # Selection filters (requirement #3). Empty list == inactive (None).
    focus_sel = set(focus_tag_ids) if focus_tag_ids else None
    any_sel = set(any_tag_ids) if any_tag_ids else None
    min_nec_rank = _NEC_RANK[min_necessity] if min_necessity is not None else None
    selection_active = (
        focus_sel is not None
        or any_sel is not None
        or max_priority is not None
        or min_nec_rank is not None
    )
    focus_map = (
        await _tag_ids_for_kinds(session, task_ids, {TagKind.project, TagKind.client})
        if focus_sel is not None
        else {}
    )
    anytag_map = (
        await _tag_ids_for_kinds(session, task_ids, {TagKind.generic})
        if any_sel is not None
        else {}
    )
    out: list[FeasibleTask] = []
    for t in tasks:
        if t.id in blocked:
            continue
        # Scope (UNION over the active selectors), then feasibility.
        if selection_active and not (
            (focus_sel is not None and bool(focus_map.get(t.id, set()) & focus_sel))
            or (any_sel is not None and bool(anytag_map.get(t.id, set()) & any_sel))
            or (max_priority is not None and t.priority <= max_priority)
            or (min_nec_rank is not None and _NEC_RANK[t.necessity] <= min_nec_rank)
        ):
            continue
        minutes = _effort_minutes(t)
        if minutes is None or minutes <= 0 or minutes > duration_minutes:
            continue
        if location is not None and t.location is not None and t.location != location:
            continue
        required_ctx = {n for n in tags.get(t.id, set()) if n.startswith(_CTX_PREFIX)}
        if not required_ctx.issubset(have_ctx):
            continue
        slack, bucket = _deadline_signal(t.due_date, window_start, minutes, duration_minutes)
        out.append(
            FeasibleTask(
                task_id=t.id,
                title=t.title,
                necessity=t.necessity,
                priority=t.priority,
                due_date=t.due_date,
                remaining_minutes=minutes,
                slack_minutes=slack,
                deadline_bucket=bucket,
            )
        )
    # Urgency-first total order: bucket rank (overdue/at_risk lift to the
    # top), then the existing nec/priority tiebreak. Inside the urgent
    # buckets raw slack orders by lateness; comfortable/none fall back to
    # a +inf sentinel so the non-urgent group keeps its old ordering.
    out.sort(
        key=lambda f: (
            _BUCKET_RANK[f.deadline_bucket],
            _NEC_RANK[f.necessity],
            f.priority,
            f.slack_minutes if f.deadline_bucket in _SLACK_ORDERED else _SLACK_SENTINEL,
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
