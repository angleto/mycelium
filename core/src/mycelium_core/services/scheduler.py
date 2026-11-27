"""Deterministic resource-aware scheduler (docs/adr/0004 + 0025, FR-4).

Stage 1: a logical CPM forward/backward pass over working calendars ->
ES/EF/LS/LF, slack, ``on_logical_critical_path``. This assumes infinite
resources and is the heuristic *input*, not the final answer (ADR-0025).
What the dates mean (task 27dc7e68, ADR-0004 amendment of 2026-11-27):

- A task with subtasks in scope is a span, not work: its own estimate is
  not scheduled, it starts with its first subtask and finishes with its
  last. A dependency into or out of it holds for the span (see
  ``_plan_graph``).
- A task with no estimate has an unknown duration, not a duration of 0.
  It is placed as a point, its row says the estimate is missing, and
  every date downstream of it is flagged as a lower bound.
- A due date bounds the latest finish of its task and, through the
  backward pass, of everything before it. Slack is signed
  (``services/slack.py``) and each row says by how much its task meets
  or misses its own date in the leveled plan.

Stage 2 (resource-constrained list scheduling, ADR-0025 P1):
- **Humans** are serial unit-capacity resources on their working
  calendar; consecutive distinct tasks for a person cost the human
  executor's ``context_switch_cost_minutes``. Fixed events are avoided
  (no-ubiquity). Order = the selected policy's priority rule.
- **LLM agents** are a K-parallel pool (per ``llm_agent`` executor's
  ``max_parallel``), 24/7 (no working calendar, no daily cap); a task
  starts at max(its precedence-driven earliest, the earliest freed pool
  slot). Cost is projected against the agent's ``credit_rate_per_hour``
  and flagged (not dropped) past ``credit_budget`` -- P1 projects only.

The objective is **multi-policy** (``SchedulePolicy``), selected per
recompute. Outputs additionally include the projected makespan and the
projected credit cost. The "resource-aware critical chain" that used to
be derived here was removed: measured on production, its 258 tasks did
not contain the critical path it claimed to extend, and nothing outside
the Schedule view read it.

Manual/pinned and in-progress tasks survive recompute. Deterministic:
stable ordering and an id final tie-break everywhere -- a
non-deterministic schedule is a failure (it is the product's
deterministic core).
"""

from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import heapq
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import Row, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.config import get_settings
from mycelium_core.errors import DomainError
from mycelium_core.i18n import MessageCode
from mycelium_core.models.ai_assistant import AiAssistant
from mycelium_core.models.calendar import CalendarHoliday, UserCalendar, WorkingCalendar
from mycelium_core.models.dependency import DependencyType, TaskDependency
from mycelium_core.models.executor import Executor, ExecutorKind
from mycelium_core.models.identity import Identity
from mycelium_core.models.membership import Role
from mycelium_core.models.schedule import Schedule
from mycelium_core.models.task import ExecKind, ScheduleMode, SchedulePolicy, Task
from mycelium_core.models.task_collaborator import TaskCollaborator
from mycelium_core.models.task_participant import TaskParticipant
from mycelium_core.models.task_tag import TaskTag
from mycelium_core.models.workflow import WorkflowState
from mycelium_core.services import audit
from mycelium_core.services import executors as executors_svc
from mycelium_core.services import identities as identities_svc
from mycelium_core.services.calendar import WorkCalendar, build_work_calendar
from mycelium_core.services.rbac import require_role
from mycelium_core.services.slack import slack_minutes

# Fixed, stable set of ``Schedule.unassignable_reason`` strings (English,
# enum-ish). Never free-form prose. This list is where the set is written
# down (ADR-0025 names the mechanism, not the values), and the SPA labels
# each one under ``scheduler.unreason``.
#  - no_capable_agent: no enabled llm_agent advertises every capability
#    the task requires (or there is no enabled llm_agent at all).
#  - budget_exhausted: every eligible enabled agent would exceed its
#    credit_budget if this task were placed on it within the horizon.
#  - assignee_inactive: the task is addressed to an ai_assistant that has
#    since been deactivated. Same class of event as deleting the agent
#    that could do the work, which already lands here: the scheduler
#    flags a visible dispatch gap, it never silently reroutes.
_UNASSIGNABLE_NO_CAPABLE_AGENT = "no_capable_agent"
_UNASSIGNABLE_BUDGET_EXHAUSTED = "budget_exhausted"
#  - estimate_missing: the task has no estimate. Admission checks each
#    agent's credit budget against effort times rate, and with no effort
#    that check passes whatever the task costs; placing it would also hold
#    a slot for no time. Before 2026-11-27 (task 238a76da) such a task got
#    no executor and no flag, so the dispatch tick never took it and
#    nothing said why.
_UNASSIGNABLE_ASSIGNEE_INACTIVE = "assignee_inactive"
_UNASSIGNABLE_ESTIMATE_MISSING = "estimate_missing"


# A task with subtasks in scope enters the CPM graph as two zero-length
# nodes, its start and its finish, with ids derived from its own. The task
# itself is not a node: its row is read off these two and its subtasks.
_ROLE_TASK = "task"
_ROLE_START = "start"
_ROLE_FINISH = "finish"


@dataclass
class _Node:
    task: Task
    assignee: uuid.UUID | None
    duration_min: int
    terminal: bool
    role: str = _ROLE_TASK
    # No estimate, so ``duration_min`` is 0 by default and not by fact.
    estimate_missing: bool = False
    # This node's dates depend on a duration nobody knows: its own or one
    # upstream of it. They are the earliest the work could be, no more.
    lower_bound: bool = False
    # A due date bounds this node's latest finish: its own, or one of a
    # task after it. Then ``lf`` is a deadline and the priority rule reads
    # it as one.
    due_bound: bool = False
    # docs/adr/0028: the routing kind (human vs llm_agent) is no
    # longer stored on the task; it comes from the joined
    # ``identities.kind``. We materialise it here so the rest of the
    # scheduler logic reads ``n.kind`` instead of
    # ``n.task.executor_kind``.
    kind: ExecKind = ExecKind.human
    es: dt.datetime = field(default=dt.datetime.min)
    ef: dt.datetime = field(default=dt.datetime.min)
    ls: dt.datetime = field(default=dt.datetime.min)
    lf: dt.datetime = field(default=dt.datetime.min)
    # Resource-leveled placement (Stage 2): the final scheduled start/end.
    ss: dt.datetime = field(default=dt.datetime.min)
    se: dt.datetime = field(default=dt.datetime.min)
    # Admission-control dispatch result (P2). ``assigned`` is the chosen
    # llm_agent (None for human/off-timeline/unassignable rows);
    # ``unassignable_reason`` is set (from the fixed set) iff this llm
    # task could not be dispatched.
    assigned: uuid.UUID | None = None
    unassignable_reason: str | None = None


@dataclass
class RecomputeSummary:
    """What a recompute produced: row count + the comparable projections
    (makespan, credit cost), the policy that produced them, the count of
    llm tasks with no admissible executor (P2 dispatch gaps), and what
    the dates rest on: how many tasks have no estimate (every date
    downstream of one is a lower bound), how many miss their due date,
    and how many dependencies were left out because they contradict the
    parent/subtask structure."""

    count: int
    makespan_minutes: int
    projected_credit_cost: Decimal
    policy: SchedulePolicy
    unassignable_count: int = 0
    unestimated_count: int = 0
    due_missed_count: int = 0
    dependencies_ignored: int = 0


def _reuse_summary(
    prev: dict[uuid.UUID, Schedule],
    *,
    ids: list[uuid.UUID],
    fp: str,
    now: dt.datetime,
    policy: SchedulePolicy,
    dependencies_ignored: int,
) -> RecomputeSummary | None:
    """The stored plan's summary when it can stand for a fresh one, else None.

    It can when the stored rows are exactly the tasks in scope, every row
    was written from the inputs ``fp`` digests, and the oldest row is
    younger than ``schedule_reuse_max_age_seconds``.

    Why reuse at all: the dispatch tick asks for a plan every
    ``dispatch_loop_interval_seconds`` in every workspace, and before this
    every ask deleted and rewrote one row per task plus an audit record:
    1605 rows a minute in the production workspace, as read through MCP on
    2026-11-26, whether or not anything had changed.

    Why an age bound: the clock is the one input the digest leaves out.
    Dates are anchored at the moment of computation, so a reused plan's
    dates fall behind by its age. What the tick reads from the plan, the
    admitted agent tasks and their order, does not move with a uniform
    shift of the clock; the dates do, and they are what a person reads.
    The bound caps that lag, and a person asking for a recompute never
    gets a reused plan.
    """
    if not prev or set(prev) != set(ids):
        return None
    rows = list(prev.values())
    if any(r.input_fingerprint != fp for r in rows):
        return None
    max_age = dt.timedelta(seconds=get_settings().schedule_reuse_max_age_seconds)
    if min(r.computed_at for r in rows) < now - max_age:
        return None
    ends = [r.scheduled_end for r in rows if not r.unassignable and r.scheduled_end is not None]
    makespan_end = max(ends, default=now)
    return RecomputeSummary(
        count=len(rows),
        makespan_minutes=max(0, int((makespan_end - now).total_seconds() // 60)),
        projected_credit_cost=sum((r.projected_cost for r in rows), Decimal(0)),
        policy=policy,
        unassignable_count=sum(1 for r in rows if r.unassignable),
        unestimated_count=sum(1 for r in rows if r.estimate_missing),
        due_missed_count=sum(
            1 for r in rows if r.due_margin_minutes is not None and r.due_margin_minutes < 0
        ),
        dependencies_ignored=dependencies_ignored,
    )


def _slack(n: _Node) -> int:
    return slack_minutes(n.lf, n.ef)


def _policy_key(
    policy: SchedulePolicy,
    rate_of: Callable[[_Node], Decimal],
) -> Callable[[_Node], tuple[object, ...]]:
    """The deterministic priority/break rule for the selected policy.

    Every key ends with ``str(task.id)`` so ordering is total and stable
    regardless of equal leading fields (the deterministic-core
    contract). ``priority`` is P1..P4 with P1 (=1) highest, so ascending
    priority = most important first.

    - ``balanced`` (default, the pre-P1 ADR-0004 rule): most important
      first -> (priority, due, created, id).
    - ``fastest`` (compress makespan): least logical slack first, then
      logical-critical first, then the balanced rule -> tasks that bind
      the makespan are placed earliest.
    - ``cheapest`` (defer LLM credit spend): zero-credit executors
      first (prefer free human/zero-rate work; push paid-LLM work
      later where the deadline still allows), then the balanced rule.
    - ``throughput`` (maximize concurrent fill): shortest effort first
      so more tasks fit per unit time, then the balanced rule.
    """
    # Sort sentinel: due_date is timestamptz (migration 0005), so a
    # missing deadline still needs a value comparable to a real
    # datetime. ``dt.datetime.max`` lacks tzinfo and would raise on
    # comparison with the aware values returned by SQLAlchemy.
    _DUE_MAX = dt.datetime.max.replace(tzinfo=dt.UTC)

    def _deadline(x: _Node) -> dt.datetime:
        # The earliest-due-date rule reads the latest finish that due dates
        # impose, the task's own or one inherited from a task after it, not
        # only the task's own date: a task with no date that an urgent task
        # waits for is as urgent as the work it holds up. Measured on
        # 2026-11-27 (test_due_date_is_a_finish_constraint_with_its_margin):
        # once successors wait for their predecessors' placement, ranking
        # by the own date alone put that predecessor after unrelated work
        # and the urgent task missed by 300 minutes instead of 180. A task
        # with no date anywhere after it ranks last, as before.
        return x.lf if x.due_bound else _DUE_MAX

    if policy is SchedulePolicy.fastest:
        return lambda x: (
            _slack(x),
            0 if _slack(x) <= 0 else 1,
            x.task.priority,
            _deadline(x),
            x.task.created_at,
            str(x.task.id),
        )
    if policy is SchedulePolicy.cheapest:
        return lambda x: (
            1 if rate_of(x) > 0 else 0,
            x.task.priority,
            _deadline(x),
            x.task.created_at,
            str(x.task.id),
        )
    if policy is SchedulePolicy.throughput:
        return lambda x: (
            x.duration_min,
            x.task.priority,
            _deadline(x),
            x.task.created_at,
            str(x.task.id),
        )
    # balanced: the canonical ADR-0004 priority rule.
    return lambda x: (
        x.task.priority,
        _deadline(x),
        x.task.created_at,
        str(x.task.id),
    )


def _effort_minutes(task: Task) -> int | None:
    """Remaining effort in minutes, or None when nobody estimated it."""
    hours = task.remaining_effort_h
    if hours is None:
        hours = task.estimate_effort_h
    if hours is None:
        return None
    return round(float(hours) * 60)


class _Timeline:
    """One person's booked time: appointments, pins and the work placed so
    far, as disjoint intervals in time order.

    Work is placed in the first stretch at or after its gate where it fits,
    so a task placed late in the pass can still go into a free morning that
    an earlier task left behind; placing only after the last booked task
    would leave that morning empty once tasks wait for their predecessors.
    Two intervals with no working time between them (Friday 18:00 and
    Monday 09:00) are kept as one, so the search walks real gaps rather
    than nights: a person with a thousand back-to-back tasks is one
    interval, not a thousand.

    The context switch cost is paid before every task after the person's
    first placed one, as it was on the cursor this replaces; it does not
    look at what the task is placed next to.
    """

    def __init__(self, cal: WorkCalendar) -> None:
        self._cal = cal
        self._starts: list[dt.datetime] = []
        self._ends: list[dt.datetime] = []
        self._placed = False

    def occupy(self, start: dt.datetime, end: dt.datetime) -> None:
        snap = self._cal.snap_forward
        i = bisect.bisect_left(self._starts, start)
        if i > 0 and snap(self._ends[i - 1]) >= start:
            i -= 1
            start = self._starts[i]
            end = max(end, self._ends[i])
            del self._starts[i], self._ends[i]
        while i < len(self._starts) and self._starts[i] <= snap(end):
            end = max(end, self._ends[i])
            del self._starts[i], self._ends[i]
        self._starts.insert(i, start)
        self._ends.insert(i, end)

    def place(
        self, gate: dt.datetime, minutes: int, cap: float, *, switch_min: int
    ) -> tuple[dt.datetime, dt.datetime]:
        cal = self._cal
        start = cal.snap_forward(gate)
        while True:
            s = start
            if self._placed and switch_min > 0:
                # Context switch: lose switch_min of working time before
                # the next distinct task can start.
                s = cal.snap_forward(cal.add(s, switch_min))
            end = cal.add_capped(s, minutes, cap)
            i = bisect.bisect_right(self._ends, s)
            if i < len(self._starts) and self._starts[i] < end:
                start = cal.snap_forward(self._ends[i])
                continue
            self.occupy(s, end)
            self._placed = True
            return s, end


def _earliest_free_slot(free: list[dt.datetime]) -> int:
    """Index of the slot that frees earliest (deterministic: smallest
    free time, lowest slot index on a tie)."""
    return min(range(len(free)), key=lambda i: (free[i], i))


def _agent_rank(
    agent: Executor, free: list[dt.datetime], earliest: dt.datetime
) -> tuple[Decimal, dt.datetime, str]:
    """Deterministic admission rank for an eligible+admissible agent:
    lowest ``credit_rate_per_hour``, then the agent's earliest effective
    free time (most free capacity / earliest-free), then
    ``str(agent.id)`` as the final stable tie-break."""
    slot_free = min(max(earliest, f) for f in free)
    return (agent.credit_rate_per_hour, slot_free, str(agent.id))


def _manual_pin_start(task: Task, prev: dict[uuid.UUID, Schedule]) -> dt.datetime | None:
    """Pinned start that must survive recompute (schedule_mode=manual,
    previously placed). Single source of truth for both passes."""
    if task.schedule_mode is not ScheduleMode.manual:
        return None
    row = prev.get(task.id)
    return row.scheduled_start if row is not None else None


def _topo(
    ids: list[uuid.UUID],
    deps: Sequence[TaskDependency | _Edge],
) -> list[uuid.UUID]:
    succ: dict[uuid.UUID, set[uuid.UUID]] = {i: set() for i in ids}
    indeg: dict[uuid.UUID, int] = {i: 0 for i in ids}
    for d in deps:
        if d.successor_id not in succ[d.predecessor_id]:
            succ[d.predecessor_id].add(d.successor_id)
            indeg[d.successor_id] += 1
    ready = sorted(i for i in ids if indeg[i] == 0)
    order: list[uuid.UUID] = []
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for s in sorted(succ[nid]):
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)
        ready.sort()
    if len(order) != len(ids):
        raise DomainError(MessageCode.DEPENDENCY_CYCLE)
    return order


@dataclass(frozen=True)
class _Edge:
    """A precedence edge of the CPM graph. The attribute names are the
    ones ``TaskDependency`` uses, so the passes read either."""

    predecessor_id: uuid.UUID
    successor_id: uuid.UUID
    type: DependencyType
    lag_working_minutes: int


@dataclass
class _PlanGraph:
    leaves: list[uuid.UUID]
    # Parent -> its direct subtasks in scope, each list in id order.
    children: dict[uuid.UUID, list[uuid.UUID]]
    start_of: dict[uuid.UUID, uuid.UUID]
    finish_of: dict[uuid.UUID, uuid.UUID]
    edges: list[_Edge]
    dependencies_ignored: int

    @property
    def node_ids(self) -> list[uuid.UUID]:
        return [*self.leaves, *self.start_of.values(), *self.finish_of.values()]

    def leaf_descendants(self, parent: uuid.UUID) -> list[uuid.UUID]:
        out: list[uuid.UUID] = []
        stack = [parent]
        while stack:
            for c in self.children[stack.pop()]:
                (stack if c in self.children else out).append(c)
        return out


def _reaches(adj: dict[uuid.UUID, set[uuid.UUID]], src: uuid.UUID, dst: uuid.UUID) -> bool:
    seen = {src}
    stack = [src]
    while stack:
        cur = stack.pop()
        if cur == dst:
            return True
        for nxt in adj.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def _plan_graph(tasks: list[Task], deps: list[TaskDependency]) -> _PlanGraph:
    """The CPM graph of a scope: its leaf tasks, and each parent as a start
    node and a finish node around its subtasks.

    A parent P is in the graph as ``start(P)`` and ``finish(P)``. Every
    direct subtask C starts no earlier than ``start(P)`` (SS 0) and
    ``finish(P)`` is no earlier than C's finish (FF 0), so P spans its
    subtasks and its own estimate is never scheduled: measured 2026-11-26,
    the one critical task on production was an epic whose 624 h estimate
    was added on top of its subtasks. A dependency touching P attaches to
    the end of P it names: FS and SS into P hold P's start, FF and SF into
    P hold its finish; FS and FF out of P read its finish, SS and SF read
    its start. This is exact. The alternative of copying each dependency
    onto every subtask is exact for FS into and out of a parent but
    over-constrains SS out of it and FF into it (every subtask would be
    held, where the span only needs its first or last one).

    Only parents and subtasks both in scope count: in a project-scoped
    plan a subtask whose parent carries another project is a plain task.

    Some dependencies cannot hold once a parent spans its subtasks: one
    between a task and its own ancestor, or a chain that leaves a span
    and comes back into it. In the plain dependency graph they are not
    cycles, so the dependency service accepts them, but here they are.
    They are left out, in dependency-id order, and counted: one bad link
    must not stop the plan of a whole workspace, and the count says it
    happened. With no such dependency, which is the case the first
    ``_topo`` attempt finds, no reachability search runs. When one does,
    it is one search per declared dependency: on a synthetic workspace of
    production's size (1605 tasks, 394 parents, 700 dependencies, 4 of
    them contradictory) the whole function took 319 ms on 2026-11-27,
    once per dispatch tick of 60 s.
    """
    by_id = {t.id: t for t in tasks}
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for t in sorted(tasks, key=lambda x: str(x.id)):
        p = t.parent_task_id
        if p is not None and p != t.id and p in by_id:
            children.setdefault(p, []).append(t.id)
    start_of = {p: uuid.uuid5(p, _ROLE_START) for p in children}
    finish_of = {p: uuid.uuid5(p, _ROLE_FINISH) for p in children}

    def entry(tid: uuid.UUID) -> uuid.UUID:
        return start_of.get(tid, tid)

    def exit_(tid: uuid.UUID) -> uuid.UUID:
        return finish_of.get(tid, tid)

    structural = [
        e
        for p, kids in children.items()
        for c in kids
        for e in (
            _Edge(start_of[p], entry(c), DependencyType.SS, 0),
            _Edge(exit_(c), finish_of[p], DependencyType.FF, 0),
        )
    ]
    declared = [
        _Edge(
            entry(d.predecessor_id)
            if d.type in (DependencyType.SS, DependencyType.SF)
            else exit_(d.predecessor_id),
            entry(d.successor_id)
            if d.type in (DependencyType.FS, DependencyType.SS)
            else exit_(d.successor_id),
            d.type,
            d.lag_working_minutes,
        )
        for d in sorted(deps, key=lambda x: str(x.id))
    ]
    leaves = sorted((t.id for t in tasks if t.id not in children), key=str)
    graph = _PlanGraph(
        leaves=leaves,
        children=children,
        start_of=start_of,
        finish_of=finish_of,
        edges=structural + declared,
        dependencies_ignored=0,
    )
    if not children:
        return graph
    try:
        _topo(graph.node_ids, graph.edges)
        return graph
    except DomainError:
        pass
    adj: dict[uuid.UUID, set[uuid.UUID]] = {}
    for e in structural:
        adj.setdefault(e.predecessor_id, set()).add(e.successor_id)
    kept: list[_Edge] = []
    for e in declared:
        if _reaches(adj, e.successor_id, e.predecessor_id):
            graph.dependencies_ignored += 1
            continue
        adj.setdefault(e.predecessor_id, set()).add(e.successor_id)
        kept.append(e)
    graph.edges = structural + kept
    return graph


class Scheduler:
    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self._s = session
        self._org = org_id
        self._cal: dict[uuid.UUID | None, tuple[WorkCalendar, float]] = {}

    async def _calendar(self, assignee: uuid.UUID | None) -> tuple[WorkCalendar, float]:
        if assignee not in self._cal:
            wc, cap = await build_work_calendar(self._s, self._org, assignee)
            self._cal[assignee] = (wc, float(cap) * 60.0)
        return self._cal[assignee]

    async def _input_fingerprint(
        self,
        *,
        policy: SchedulePolicy,
        tasks: list[Task],
        deps: list[TaskDependency],
        terminal_state_ids: set[uuid.UUID],
        assignee_rows: Sequence[Row[tuple[uuid.UUID, uuid.UUID]]],
        identity_rows: Sequence[Row[Any]],
        exec_rows: list[Executor],
        participant_rows: Sequence[Row[Any]],
    ) -> str:
        """A digest of everything the plan is computed from, except the clock.

        Every table ``recompute`` reads goes in, in a deterministic order:
        the tasks, their dependencies, which states are terminal, the
        assignees and the identities they resolve to, the appointment
        participants, the executors, and the working calendars with their
        holidays and per-user overrides. Versioned rows enter as
        ``id:version:updated_at``. ``updated_at`` is there because the ORM
        moves it on every UPDATE it issues (``onupdate`` on
        ``TimestampMixin``) while the version is moved by each service by
        hand, so an ORM write path that forgets the version is still seen.
        A raw SQL UPDATE that sets neither is not; the age bound is the
        most such a write can go unseen.

        The clock is left out on purpose: a plan's dates are relative to
        the moment it was computed, so a digest that included it would
        never repeat and nothing could be reused. The age bound in
        ``_reuse_summary`` is what keeps the dates from drifting.

        The previous rows are an input too (a manual pin is read from the
        stored schedule), but they are the plan this digest is compared
        against, so they cannot change without the digest changing first.
        """
        cal_rows = (
            await self._s.execute(select(WorkingCalendar).order_by(WorkingCalendar.id))
        ).scalars()
        holiday_rows = (
            await self._s.execute(
                select(CalendarHoliday.calendar_id, CalendarHoliday.day).order_by(
                    CalendarHoliday.calendar_id, CalendarHoliday.day
                )
            )
        ).all()
        user_cal_rows = (
            await self._s.execute(select(UserCalendar).order_by(UserCalendar.user_id))
        ).scalars()

        def _v(row: Any) -> str:
            return f"{row.id}:{row.version}:{row.updated_at.isoformat()}"

        parts: list[str] = [f"policy:{policy.value}"]
        parts += [f"t:{_v(t)}" for t in sorted(tasks, key=lambda x: str(x.id))]
        parts += [
            f"d:{_v(d)}:{d.predecessor_id}:{d.successor_id}:{d.type.value}:{d.lag_working_minutes}"
            for d in sorted(deps, key=lambda x: str(x.id))
        ]
        parts += [f"term:{sid}" for sid in sorted(terminal_state_ids, key=str)]
        parts += [
            f"a:{tid}:{uid}"
            for tid, uid in sorted(assignee_rows, key=lambda r: (str(r[0]), str(r[1])))
        ]
        parts += [
            "i:" + ":".join(str(v) for v in row)
            for row in sorted(identity_rows, key=lambda r: str(r[0]))
        ]
        parts += [
            "p:" + ":".join(str(v) for v in row)
            for row in sorted(participant_rows, key=lambda r: tuple(str(v) for v in r))
        ]
        parts += [f"e:{_v(e)}" for e in sorted(exec_rows, key=lambda x: str(x.id))]
        parts += [f"c:{_v(c)}" for c in cal_rows]
        parts += [f"h:{cid}:{day.isoformat()}" for cid, day in holiday_rows]
        parts += [
            f"uc:{u.user_id}:{u.calendar_id}:{u.version}:{u.updated_at.isoformat()}"
            for u in user_cal_rows
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    async def recompute(
        self,
        *,
        actor_id: uuid.UUID,
        project_tag_id: uuid.UUID | None = None,
        as_of: dt.datetime | None = None,
        policy: SchedulePolicy = SchedulePolicy.balanced,
        reuse_unchanged: bool = False,
    ) -> RecomputeSummary:
        """Compute and store the plan for the workspace (or one project).

        ``reuse_unchanged`` is for the periodic dispatch tick: when every
        input the plan reads is what the stored plan was computed from, and
        that plan is younger than ``schedule_reuse_max_age_seconds``, the
        stored rows are kept and summarised instead of being deleted and
        written again. See ``_input_fingerprint`` for what counts as an
        input. A person asking for a recompute gets a fresh one.
        """
        await require_role(self._s, self._org, actor_id, Role.member)
        now = (as_of or dt.datetime.now(tz=dt.UTC)).astimezone(dt.UTC)

        # Lazy + idempotent: a fresh workspace gets its executor rows
        # (human-per-member + the default LLM pool) with zero manual
        # config (defaults make this a no-op vs the pre-P1 scheduler).
        await executors_svc.ensure_workspace_executors(self._s, org_id=self._org)

        stmt = select(Task).where(Task.deleted_at.is_(None), Task.is_archived.is_(False))
        if project_tag_id is not None:
            stmt = stmt.join(TaskTag, TaskTag.task_id == Task.id).where(
                TaskTag.tag_id == project_tag_id
            )
        tasks = list((await self._s.execute(stmt)).scalars().unique().all())
        if not tasks:
            return RecomputeSummary(
                count=0,
                makespan_minutes=0,
                projected_credit_cost=Decimal(0),
                policy=policy,
                unassignable_count=0,
            )
        ids = [t.id for t in tasks]
        id_set = set(ids)

        deps = [
            d
            for d in (await self._s.execute(select(TaskDependency))).scalars().all()
            if d.predecessor_id in id_set and d.successor_id in id_set
        ]
        graph = _plan_graph(tasks, deps)
        terminal_state_ids = set(
            (
                await self._s.execute(
                    select(WorkflowState.id).where(WorkflowState.is_terminal.is_(True))
                )
            )
            .scalars()
            .all()
        )
        assignee_rows = (
            await self._s.execute(
                select(TaskCollaborator.task_id, TaskCollaborator.user_id).order_by(
                    TaskCollaborator.user_id
                )
            )
        ).all()
        first_assignee: dict[uuid.UUID, uuid.UUID] = {}
        for tid, uid in assignee_rows:
            first_assignee.setdefault(tid, uid)

        # docs/adr/0028: resolve the task's primary assignee through
        # identities. ``assignee_kind`` per task is derived from
        # ``identities.kind``; ``assignee_human_user_id`` is the
        # ``identities.user_id`` for human identities (NULL for
        # ai_assistants and for unassigned tasks).
        identity_rows = (
            await self._s.execute(
                select(
                    Task.id,
                    Identity.kind,
                    Identity.user_id,
                    AiAssistant.is_active,
                    AiAssistant.runtime,
                )
                .join(Identity, Identity.id == Task.assignee_id)
                .outerjoin(AiAssistant, AiAssistant.id == Identity.ai_assistant_id)
            )
        ).all()
        task_kind: dict[uuid.UUID, ExecKind] = {}
        task_human_user: dict[uuid.UUID, uuid.UUID | None] = {}
        # Tasks addressed to a deactivated assistant. Kept in the llm pool
        # (the assignment is a fact about the task) and flagged
        # ``assignee_inactive`` at admission, rather than dropped from
        # routing: an excluded task would keep a schedule row claiming it
        # is planned and on time while nothing ever dispatches it.
        self._assignee_inactive: set[uuid.UUID] = set()
        for tid, ikind, iuser, a_active, a_runtime in identity_rows:
            if identities_svc.is_internal_agent(ikind, a_runtime):
                task_kind[tid] = ExecKind.llm_agent
                task_human_user[tid] = None
                if a_active is not True:
                    self._assignee_inactive.add(tid)
            else:
                # An EXTERNAL assistant lands here with the humans, and
                # its ``task_human_user`` stays NULL: nothing on this
                # side of the system starts it, so it gets no executor,
                # no projected cost and no scheduled window, and
                # ``_admitted_agent_rows`` never sees it because that
                # query requires ``assigned_executor_id IS NOT NULL``.
                # Off-calendar rather than mis-scheduled -- unless a
                # task_assignee row names a person, in which case the
                # task is that person's planned work and belongs on
                # their calendar.
                task_kind[tid] = ExecKind.human
                task_human_user[tid] = iuser

        prev = {
            row.task_id: row
            for row in (await self._s.execute(select(Schedule))).scalars().all()
            if row.task_id in id_set
        }

        # Loaded here rather than where they are used so that the
        # fingerprint and the computation read the same rows.
        exec_rows = list(
            (
                await self._s.execute(
                    select(Executor).order_by(Executor.kind, Executor.name, Executor.id)
                )
            )
            .scalars()
            .all()
        )
        participant_rows = (
            await self._s.execute(
                select(
                    Identity.user_id,
                    TaskParticipant.start_at,
                    TaskParticipant.duration_minutes,
                )
                .join(Identity, Identity.id == TaskParticipant.identity_id)
                .where(Identity.user_id.is_not(None))
            )
        ).all()
        fp = await self._input_fingerprint(
            policy=policy,
            tasks=tasks,
            deps=deps,
            terminal_state_ids=terminal_state_ids,
            assignee_rows=assignee_rows,
            identity_rows=identity_rows,
            exec_rows=exec_rows,
            participant_rows=participant_rows,
        )
        if reuse_unchanged:
            reused = _reuse_summary(
                prev,
                ids=ids,
                fp=fp,
                now=now,
                policy=policy,
                dependencies_ignored=graph.dependencies_ignored,
            )
            if reused is not None:
                return reused

        nodes: dict[uuid.UUID, _Node] = {}
        by_task = {t.id: t for t in tasks}
        for tid in graph.leaves:
            t = by_task[tid]
            # Three fallbacks for the human executor id (used by the
            # calendar resolver / serial-resource scheduler): the
            # first task_assignee row -> the identity-resolved human
            # user -> NULL (unassigned, off-calendar).
            assignee = first_assignee.get(t.id) or task_human_user.get(t.id)
            terminal = t.state_id in terminal_state_ids
            effort = _effort_minutes(t)
            # Nothing is missing for a milestone (no duration by
            # definition), a closed task (no work left) or an appointment
            # (its own window is its duration).
            missing = (
                effort is None
                and not t.is_milestone
                and not terminal
                and t.duration_minutes is None
            )
            dur = 0 if t.is_milestone or effort is None else effort
            # Kind via identity when an assignee is set, else fall
            # back to the task's ``executor_kind`` hint (docs/adr/0028).
            n_kind = task_kind.get(t.id, t.executor_kind)
            nodes[t.id] = _Node(
                task=t,
                assignee=assignee,
                duration_min=dur,
                terminal=terminal,
                kind=n_kind,
                estimate_missing=missing,
            )
        for parent_id, vid in [*graph.start_of.items(), *graph.finish_of.items()]:
            t = by_task[parent_id]
            nodes[vid] = _Node(
                task=t,
                assignee=first_assignee.get(parent_id) or task_human_user.get(parent_id),
                duration_min=0,
                terminal=False,
                kind=task_kind.get(parent_id, t.executor_kind),
                role=_ROLE_START if vid == graph.start_of[parent_id] else _ROLE_FINISH,
            )
        graph_ids = graph.node_ids

        incoming: dict[uuid.UUID, list[_Edge]] = {i: [] for i in graph_ids}
        outgoing: dict[uuid.UUID, list[_Edge]] = {i: [] for i in graph_ids}
        for edge in graph.edges:
            incoming[edge.successor_id].append(edge)
            outgoing[edge.predecessor_id].append(edge)

        order = _topo(graph_ids, graph.edges)

        # Forward pass: ES/EF.
        for nid in order:
            n = nodes[nid]
            cal, cap = await self._calendar(n.assignee)
            n.lower_bound = n.estimate_missing or any(
                nodes[d.predecessor_id].lower_bound for d in incoming[nid]
            )
            if n.role == _ROLE_FINISH:
                # A span ends when its last subtask does (or later, if a
                # finish dependency into the parent says so). Taken as is,
                # not snapped: snapping a finish at 18:00 forward would end
                # the parent at 09:00 the next morning.
                bounds: list[dt.datetime] = []
                for d in incoming[nid]:
                    p = nodes[d.predecessor_id]
                    pcal, _ = await self._calendar(p.assignee)
                    ref = p.ef if d.type is DependencyType.FF else p.es
                    lag = d.lag_working_minutes
                    bounds.append(ref if lag == 0 else pcal.add(ref, lag))
                n.es = n.ef = max(bounds, default=now)
                continue
            if n.terminal:
                anchor = n.task.actual_start or now
                n.es = anchor
                n.ef = anchor
                # Closed: its dates are facts, whatever came before it.
                n.lower_bound = False
                continue
            start_lb = [now]
            finish_lb: list[dt.datetime] = []
            cd = n.task.constraint_date
            ck = n.task.constraint_kind.value
            if n.role == _ROLE_START:
                # A parent's start constraints hold its span: MSO is read
                # as "no subtask before", since the span starts with its
                # first subtask and cannot be made to start on a date by
                # itself. MFO and a manual pin are not applied to a
                # parent: its finish and its dates are its subtasks'.
                if cd is not None and ck in ("SNET", "MSO"):
                    start_lb.append(cd)
                for d in incoming[nid]:
                    p = nodes[d.predecessor_id]
                    pcal, _ = await self._calendar(p.assignee)
                    ref = p.ef if d.type is DependencyType.FS else p.es
                    start_lb.append(pcal.add(ref, d.lag_working_minutes))
                n.es = n.ef = cal.snap_forward(max(start_lb))
                continue
            if n.task.actual_start is not None:
                start_lb.append(n.task.actual_start)
            if cd is not None and ck == "SNET":
                start_lb.append(cd)
            for d in incoming[nid]:
                p = nodes[d.predecessor_id]
                pcal, _ = await self._calendar(p.assignee)
                lag = d.lag_working_minutes
                if d.type is DependencyType.FS:
                    start_lb.append(pcal.add(p.ef, lag))
                elif d.type is DependencyType.SS:
                    start_lb.append(pcal.add(p.es, lag))
                elif d.type is DependencyType.FF:
                    finish_lb.append(pcal.add(p.ef, lag))
                else:  # SF
                    finish_lb.append(pcal.add(p.es, lag))
            es = cal.snap_forward(max(start_lb))
            if finish_lb:
                need = max(finish_lb)
                es = max(es, cal.subtract(need, n.duration_min))
            if cd is not None and ck == "MSO":
                es = cd
            pin = _manual_pin_start(n.task, prev)
            if pin is not None:
                es = pin
            if cd is not None and ck == "MFO":
                n.ef = cd
                n.es = cal.subtract(cd, n.duration_min)
            else:
                n.es = es
                n.ef = es if n.duration_min == 0 else cal.add_capped(es, n.duration_min, cap)

        project_end = max(n.ef for n in nodes.values())

        # Backward pass: LF/LS. A task's own latest finish is its due date
        # when it has one (a leaf, or the finish of a parent's span), and
        # the end of the plan when it has none; its successors can only
        # bring it earlier. The end of the plan stands in for "no date",
        # so it does not bound a task that has a date: a task due on Friday
        # in a plan that ends on Monday has until Friday, which is also
        # what the advisory layer says about it. A closed task has nothing
        # left to finish, so its due date is not a bound.
        for nid in reversed(order):
            n = nodes[nid]
            cal, _ = await self._calendar(n.assignee)
            due = n.task.due_date
            if due is not None and n.role != _ROLE_START and not n.terminal:
                finish_ub = [due]
            else:
                finish_ub = [project_end]
            for d in outgoing[nid]:
                snode = nodes[d.successor_id]
                lag = d.lag_working_minutes
                if d.type is DependencyType.FS:
                    finish_ub.append(cal.subtract(snode.ls, lag))
                elif d.type is DependencyType.FF:
                    finish_ub.append(cal.subtract(snode.lf, lag))
                else:
                    # SS reads this start against the successor's start,
                    # SF against its finish (SF used ``ls`` before
                    # 2026-11-27, which is not the end SF constrains).
                    ref = snode.ls if d.type is DependencyType.SS else snode.lf
                    latest_start = cal.subtract(ref, lag)
                    finish_ub.append(
                        latest_start
                        if n.duration_min == 0
                        else cal.add(latest_start, n.duration_min)
                    )
            n.lf = min(finish_ub)
            n.ls = cal.subtract(n.lf, n.duration_min)
            n.due_bound = (due is not None and n.role != _ROLE_START and not n.terminal) or any(
                nodes[d.successor_id].due_bound for d in outgoing[nid]
            )

        # --- Stage 2: resource-constrained list scheduling (ADR-0025) ---
        # Executors were just ensured. Index humans by user (the row
        # carries only the switch penalty; the calendar is still
        # resolved by user via build_work_calendar) and resolve the SET
        # of enabled llm_agent executors (P2: capability-matched
        # admission control across multiple agents, replacing P1's single
        # default pool). Deterministic order: (kind, name, id). The rows
        # were loaded with the other inputs, above the fingerprint.
        human_exec: dict[uuid.UUID, Executor] = {}
        for e in exec_rows:
            if e.kind is ExecutorKind.human and e.user_id is not None:
                human_exec.setdefault(e.user_id, e)
        # Enabled llm_agent executors, deterministically ordered. Each is
        # an admission target: K=max_parallel concurrent slots (24/7, no
        # calendar) + an optional credit_budget.
        agents: list[Executor] = [
            e for e in exec_rows if e.kind is ExecutorKind.llm_agent and e.enabled
        ]
        agent_caps: dict[uuid.UUID, set[str]] = {a.id: set(a.capability_tags or []) for a in agents}
        # Per-agent rate, for the projected cost of a task assigned to it.
        agent_rate_by_id: dict[uuid.UUID, Decimal] = {a.id: a.credit_rate_per_hour for a in agents}

        def _effort_hours(n: _Node) -> Decimal:
            h = n.task.remaining_effort_h
            if h is None:
                h = n.task.estimate_effort_h
            return Decimal(0) if h is None else Decimal(h)

        def _eligible_agents(n: _Node) -> list[Executor]:
            """Enabled agents whose capability_tags ⊇ the task's
            required_capabilities (empty required = any enabled agent)."""
            req = set(n.task.required_capabilities or [])
            return [a for a in agents if req <= agent_caps[a.id]]

        def _node_rate(n: _Node) -> Decimal:
            # Policy-key rate proxy: an llm task's projected rate is the
            # cheapest eligible agent's rate (the dispatcher prefers the
            # lowest rate); 0 for human work or no eligible agent.
            if n.kind is not ExecKind.llm_agent:
                return Decimal(0)
            elig = _eligible_agents(n)
            if not elig:
                return Decimal(0)
            return min(a.credit_rate_per_hour for a in elig)

        key = _policy_key(policy, _node_rate)

        # One placement pass over every node of the graph (task be2fff43).
        # A node is placed once all its predecessors are, so a task starts
        # where its predecessors really are in the leveled plan, whoever
        # does them; among the nodes whose predecessors are all placed, the
        # policy's priority rule picks (a serial schedule generation
        # scheme). Before 2026-11-27 people were placed one by one in
        # priority order from their CPM earliest start, and only the agent
        # pool read a predecessor's leveled window: a person's task could
        # start a day before the task it depends on was done.
        #
        # Keyed by leaf task id. A parent is never placed: its window is
        # the span of its subtasks' placements, read off after placing.
        sched: dict[uuid.UUID, tuple[dt.datetime, dt.datetime]] = {}
        # Migration 0094 / ADR-0008 addendum: appointment-tasks
        # (``duration_minutes`` NOT NULL on the row) are pre-placed hard
        # constraints at their fixed window. Every participant (migrations
        # 0095/0096: the assignee is mirrored into ``task_participants``)
        # has the slot on their timeline, so plain work goes around it. AI
        # assistants have no users.id and no human timeline.
        timelines: dict[uuid.UUID, _Timeline] = {}

        async def _timeline(user_id: uuid.UUID) -> _Timeline:
            if user_id not in timelines:
                cal, _ = await self._calendar(user_id)
                timelines[user_id] = _Timeline(cal)
            return timelines[user_id]

        for uid, p_start, p_dur in participant_rows:
            (await _timeline(uid)).occupy(p_start, p_start + dt.timedelta(minutes=p_dur))
        # A manual pin on a person's task is fixed too: it goes on the
        # timeline before anything is placed, so later work goes around it
        # rather than over it.
        for nid in graph.leaves:
            n = nodes[nid]
            pin = _manual_pin_start(n.task, prev)
            if pin is not None and n.kind is ExecKind.human and n.assignee is not None:
                if not n.terminal and n.duration_min > 0:
                    cal, cap = await self._calendar(n.assignee)
                    (await _timeline(n.assignee)).occupy(
                        pin, cal.add_capped(pin, n.duration_min, cap)
                    )

        # LLM agents (P2 admission-control dispatch). Each enabled agent is
        # a K-parallel resource (max_parallel slots, 24/7 -- no working
        # calendar, no daily cap) with an optional credit_budget. For each
        # llm task, among its CAPABILITY-ELIGIBLE enabled agents pick the
        # admissible one (a slot free at the earliest time AND projected
        # cumulative spend on that agent + this task's cost <= its
        # credit_budget if set). A task with no admissible eligible agent
        # is FLAGGED ``unassignable`` (never silently scheduled) and keeps
        # its logical ES/EF as a visible dispatch gap.
        agent_state: dict[uuid.UUID, tuple[list[dt.datetime], list[uuid.UUID | None], Decimal]] = {}
        for a in agents:
            k = max(1, a.max_parallel)
            agent_state[a.id] = ([now] * k, [None] * k, Decimal(0))
        unassignable_ids: set[uuid.UUID] = set()

        async def _bound(d: _Edge) -> tuple[dt.datetime, bool]:
            """What one dependency asks of its successor in the leveled plan:
            the instant, and whether it bounds the successor's finish (FF,
            SF) rather than its start (FS, SS). Same reading as the forward
            pass, on the predecessor's placed window. A zero lag is the
            instant itself: snapping it onto the predecessor's calendar
            would hold an agent, which works at night, until morning."""
            p = nodes[d.predecessor_id]
            ref = p.se if d.type in (DependencyType.FS, DependencyType.FF) else p.ss
            if d.lag_working_minutes != 0:
                pcal, _ = await self._calendar(p.assignee)
                ref = pcal.add(ref, d.lag_working_minutes)
            return ref, d.type in (DependencyType.FF, DependencyType.SF)

        async def _gate(nid: uuid.UUID, n: _Node, *, wall_clock: bool) -> dt.datetime:
            """Earliest start the placed predecessors allow, never before the
            CPM earliest start (which already holds now, the constraint and
            the actual start). A finish bound becomes a start bound by the
            node's own duration: on the wall clock for an agent, on the
            working calendar for everybody else."""
            cal, _ = await self._calendar(n.assignee)
            at = n.es
            for d in incoming[nid]:
                ref, holds_finish = await _bound(d)
                if holds_finish:
                    ref = (
                        ref - dt.timedelta(minutes=n.duration_min)
                        if wall_clock
                        else cal.subtract(ref, n.duration_min)
                    )
                at = max(at, ref)
            return at

        async def _place(nid: uuid.UUID) -> None:
            n = nodes[nid]
            t = n.task
            if n.role == _ROLE_START:
                at = await _gate(nid, n, wall_clock=False)
                n.ss = n.se = at
                return
            if n.role == _ROLE_FINISH:
                bounds = [(await _bound(d))[0] for d in incoming[nid]]
                n.ss = n.se = max(bounds, default=n.ef)
                return
            if t.start_at is not None and t.duration_minutes is not None:
                n.ss, n.se = t.start_at, t.start_at + dt.timedelta(minutes=t.duration_minutes)
                sched[nid] = (n.ss, n.se)
                return
            if n.terminal:
                n.ss, n.se = n.es, n.ef
                sched[nid] = (n.ss, n.se)
                return
            pin = _manual_pin_start(t, prev)
            if n.kind is ExecKind.human and n.assignee is not None and n.duration_min > 0:
                cal, cap = await self._calendar(n.assignee)
                if pin is not None:
                    # Already on the timeline (see above); fixed.
                    n.ss, n.se = pin, cal.add_capped(pin, n.duration_min, cap)
                else:
                    switch_min = (
                        human_exec[n.assignee].context_switch_cost_minutes
                        if n.assignee in human_exec
                        else 0
                    )
                    line = await _timeline(n.assignee)
                    n.ss, n.se = line.place(
                        await _gate(nid, n, wall_clock=False),
                        n.duration_min,
                        cap,
                        switch_min=switch_min,
                    )
                sched[nid] = (n.ss, n.se)
                return
            if n.kind is ExecKind.llm_agent and (n.duration_min > 0 or n.estimate_missing):
                await _place_on_agent(nid, n, pin)
                return
            # On no timeline: no estimate, a milestone, or nobody assigned
            # who could hold it. It still waits for its predecessors.
            cal, cap = await self._calendar(n.assignee)
            at = await _gate(nid, n, wall_clock=False)
            if n.duration_min == 0:
                n.ss = n.se = at
            else:
                n.ss = cal.snap_forward(at)
                n.se = cal.add_capped(n.ss, n.duration_min, cap)
            sched[nid] = (n.ss, n.se)

        async def _place_on_agent(nid: uuid.UUID, ln: _Node, pin: dt.datetime | None) -> None:
            def _gap(reason: str) -> None:
                ln.assigned = None
                ln.unassignable_reason = reason
                unassignable_ids.add(nid)
                ln.ss, ln.se = ln.es, ln.ef
                sched[nid] = (ln.es, ln.ef)

            if nid in self._assignee_inactive:
                # Addressed to an assistant that can no longer act. Not a
                # capability problem and not a budget problem, so it gets
                # its own reason: "no_capable_agent" would send the owner
                # looking for a missing capability tag.
                _gap(_UNASSIGNABLE_ASSIGNEE_INACTIVE)
                return
            if ln.estimate_missing:
                _gap(_UNASSIGNABLE_ESTIMATE_MISSING)
                return
            elig = _eligible_agents(ln)
            if not elig:
                # No enabled agent advertises every required capability
                # (or no enabled agent at all). Flagged dispatch gap.
                _gap(_UNASSIGNABLE_NO_CAPABLE_AGENT)
                return
            eff_h = _effort_hours(ln)
            earliest = pin if pin is not None else max(await _gate(nid, ln, wall_clock=True), now)
            # Among eligible agents, those that ADMIT this task on budget
            # (no budget set = unlimited; budget set = cumulative + this
            # task's cost on that agent must not exceed it).
            admissible = [
                a
                for a in elig
                if a.credit_budget is None
                or agent_state[a.id][2] + eff_h * agent_rate_by_id[a.id] <= a.credit_budget
            ]
            if not admissible:
                _gap(_UNASSIGNABLE_BUDGET_EXHAUSTED)
                return
            # Deterministic executor pick: lowest credit_rate_per_hour,
            # then earliest-free / most free capacity (the smallest
            # earliest-free slot time across the agent's slots), then
            # str(executor.id) as the final stable tie-break.
            chosen = min(
                admissible,
                key=lambda a: _agent_rank(a, agent_state[a.id][0], earliest),
            )
            free, last, spent = agent_state[chosen.id]
            # Earliest freed slot on the chosen agent (deterministic:
            # smallest free time, lowest slot index on a tie).
            slot = _earliest_free_slot(free)
            start = max(earliest, free[slot])
            end = start + dt.timedelta(minutes=ln.duration_min)
            free[slot] = end
            last[slot] = nid
            agent_state[chosen.id] = (free, last, spent + eff_h * agent_rate_by_id[chosen.id])
            ln.assigned = chosen.id
            ln.unassignable_reason = None
            sched[nid] = (start, end)
            ln.ss, ln.se = start, end

        # Kahn's order with a priority heap: a node is ready when all its
        # predecessors are placed. A parent's start or finish node goes as
        # soon as it is ready (it takes no resource); a task goes in the
        # policy's order, which ends with the id, so the sequence is total
        # and the plan deterministic.
        def _rank(nid: uuid.UUID) -> tuple[int, tuple[object, ...], str]:
            n = nodes[nid]
            if n.role != _ROLE_TASK:
                return (0, (), str(nid))
            return (1, key(n), str(nid))

        indeg = {nid: len(incoming[nid]) for nid in graph_ids}
        ready = [(*_rank(nid), nid) for nid in graph_ids if indeg[nid] == 0]
        heapq.heapify(ready)
        while ready:
            *_, nid = heapq.heappop(ready)
            await _place(nid)
            for d in outgoing[nid]:
                indeg[d.successor_id] -= 1
                if indeg[d.successor_id] == 0:
                    heapq.heappush(ready, (*_rank(d.successor_id), d.successor_id))

        # An unassignable llm task is a dispatch gap, not a placed task:
        # it has no scheduled_start/end and does not drive the makespan.
        # Its logical ES/EF/LS/LF still emit.
        for nid in graph.leaves:
            n = nodes[nid]
            if nid not in sched:
                sched[nid] = (n.es, n.ef)
            n.ss, n.se = sched[nid]
        makespan_end = max(
            (nodes[nid].se for nid in graph.leaves if nid not in unassignable_ids),
            default=now,
        )

        await self._s.execute(delete(Schedule).where(Schedule.task_id.in_(ids)))
        total_cost = Decimal(0)
        unestimated_count = 0
        due_missed_count = 0

        def _due_margin(t: Task, finish: dt.datetime | None, terminal: bool) -> int | None:
            # By how much the leveled plan meets (+) or misses (-) the
            # task's own due date. None with no date, no placement, or
            # nothing left to do.
            if t.due_date is None or finish is None or terminal:
                return None
            return slack_minutes(t.due_date, finish)

        for t in tasks:
            if t.id in graph.children:
                span_start = nodes[graph.start_of[t.id]]
                span_finish = nodes[graph.finish_of[t.id]]
                leaves = [nodes[i] for i in graph.leaf_descendants(t.id)]
                es = min(x.es for x in leaves)
                ls = min(x.ls for x in leaves)
                ef, lf = span_finish.ef, span_finish.lf
                ss = min(sched[x.task.id][0] for x in leaves)
                se = max(sched[x.task.id][1] for x in leaves)
                terminal = t.state_id in terminal_state_ids
                margin = _due_margin(t, se, terminal)
                row = Schedule(
                    task_id=t.id,
                    org_id=self._org,
                    es=es,
                    ef=ef,
                    ls=ls,
                    lf=lf,
                    slack_minutes=slack_minutes(lf, ef),
                    on_logical_critical_path=slack_minutes(lf, ef) <= 0,
                    # A parent's work is its subtasks', and so is its
                    # cost: counting it here would count it twice.
                    projected_cost=Decimal(0),
                    scheduled_start=ss,
                    scheduled_end=se,
                    assigned_executor_id=None,
                    unassignable=False,
                    unassignable_reason=None,
                    estimate_missing=False,
                    dates_lower_bound=span_finish.lower_bound or span_start.lower_bound,
                    due_margin_minutes=margin,
                    computed_at=now,
                    input_fingerprint=fp,
                )
            else:
                n = nodes[t.id]
                ss, se = sched[t.id]
                is_unassignable = t.id in unassignable_ids
                # Projected cost = effort_h * the ASSIGNED agent's rate
                # (0 for human work or an unassignable llm task -- no
                # executor, no cost).
                if n.kind is ExecKind.llm_agent and n.assigned is not None and not is_unassignable:
                    pcost = _effort_hours(n) * agent_rate_by_id.get(n.assigned, Decimal(0))
                else:
                    pcost = Decimal(0)
                total_cost += pcost
                margin = _due_margin(t, None if is_unassignable else se, n.terminal)
                unestimated_count += n.estimate_missing
                row = Schedule(
                    task_id=t.id,
                    org_id=self._org,
                    es=n.es,
                    ef=n.ef,
                    ls=n.ls,
                    lf=n.lf,
                    slack_minutes=_slack(n),
                    on_logical_critical_path=_slack(n) <= 0,
                    projected_cost=pcost,
                    # Unassignable llm tasks have no placement: NULL
                    # scheduled_start/end (a visible dispatch gap).
                    scheduled_start=None if is_unassignable else ss,
                    scheduled_end=None if is_unassignable else se,
                    assigned_executor_id=n.assigned,
                    unassignable=is_unassignable,
                    unassignable_reason=n.unassignable_reason,
                    estimate_missing=n.estimate_missing,
                    dates_lower_bound=n.lower_bound,
                    due_margin_minutes=margin,
                    computed_at=now,
                    input_fingerprint=fp,
                )
            due_missed_count += margin is not None and margin < 0
            self._s.add(row)
        await self._s.flush()
        await audit.log(
            self._s,
            org_id=self._org,
            actor_id=actor_id,
            entity="schedule",
            entity_id=None,
            action="recompute",
        )
        makespan_minutes = max(0, int((makespan_end - now).total_seconds() // 60))
        unassignable_count = len(unassignable_ids)
        if unassignable_count:
            # Surface the dispatch gaps in the audit trail (the count is
            # also returned in the summary and per-row on the schedule).
            await audit.log(
                self._s,
                org_id=self._org,
                actor_id=actor_id,
                entity="schedule",
                entity_id=None,
                action="dispatch_unassignable",
                diff={"count": str(unassignable_count)},
            )
        return RecomputeSummary(
            count=len(tasks),
            makespan_minutes=makespan_minutes,
            projected_credit_cost=total_cost,
            policy=policy,
            unassignable_count=unassignable_count,
            unestimated_count=unestimated_count,
            due_missed_count=due_missed_count,
            dependencies_ignored=graph.dependencies_ignored,
        )


async def recompute(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_tag_id: uuid.UUID | None = None,
    as_of: dt.datetime | None = None,
    policy: SchedulePolicy = SchedulePolicy.balanced,
    reuse_unchanged: bool = False,
) -> RecomputeSummary:
    return await Scheduler(session, org_id).recompute(
        actor_id=actor_id,
        project_tag_id=project_tag_id,
        as_of=as_of,
        policy=policy,
        reuse_unchanged=reuse_unchanged,
    )


async def get_schedule(
    session: AsyncSession, *, org_id: uuid.UUID, task_id: uuid.UUID
) -> Schedule | None:
    return (
        await session.execute(select(Schedule).where(Schedule.task_id == task_id))
    ).scalar_one_or_none()


async def list_schedule(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    project_tag_id: uuid.UUID | None = None,
) -> list[Schedule]:
    if project_tag_id is None:
        return list((await session.execute(select(Schedule))).scalars().all())
    rows = (
        (
            await session.execute(
                select(Schedule)
                .join(TaskTag, TaskTag.task_id == Schedule.task_id)
                .where(TaskTag.tag_id == project_tag_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
