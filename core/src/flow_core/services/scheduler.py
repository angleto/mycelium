"""Deterministic resource-aware scheduler (docs/adr/0004 + 0025, FR-4).

Stage 1 (unchanged): a logical CPM forward/backward pass over working
calendars -> ES/EF/LS/LF, logical slack, ``on_logical_critical_path``.
This assumes infinite resources and is the heuristic *input*, not the
final answer (ADR-0025).

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
recompute. Outputs additionally include the **resource-aware critical
chain** (zero float in the *leveled* plan, distinct from the logical
critical path), the projected makespan and the projected credit cost.

Manual/pinned and in-progress tasks survive recompute. Deterministic:
stable ordering and an id final tie-break everywhere -- a
non-deterministic schedule is a failure (it is the product's
deterministic core).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.dependency import DependencyType, TaskDependency
from flow_core.models.event import Event, EventParticipant
from flow_core.models.executor import Executor, ExecutorKind
from flow_core.models.membership import Role
from flow_core.models.schedule import Schedule
from flow_core.models.task import ExecKind, ScheduleMode, SchedulePolicy, Task
from flow_core.models.task_assignee import TaskAssignee
from flow_core.models.task_tag import TaskTag
from flow_core.models.workflow import WorkflowState
from flow_core.services import audit
from flow_core.services import executors as executors_svc
from flow_core.services.calendar import WorkCalendar, build_work_calendar
from flow_core.services.rbac import require_role


@dataclass
class _Node:
    task: Task
    assignee: uuid.UUID | None
    duration_min: int
    terminal: bool
    es: dt.datetime = field(default=dt.datetime.min)
    ef: dt.datetime = field(default=dt.datetime.min)
    ls: dt.datetime = field(default=dt.datetime.min)
    lf: dt.datetime = field(default=dt.datetime.min)
    # Resource-leveled placement (Stage 2). ``ss``/``se`` are the final
    # scheduled start/end; ``llf`` is the leveled latest finish from the
    # backward pass over the leveled graph (precedence + per-resource
    # successor edges) used to derive ``on_critical_chain``.
    ss: dt.datetime = field(default=dt.datetime.min)
    se: dt.datetime = field(default=dt.datetime.min)
    llf: dt.datetime = field(default=dt.datetime.min)


@dataclass
class RecomputeSummary:
    """What a recompute produced: row count + the comparable projections
    (makespan, credit cost) and the policy that produced them."""

    count: int
    makespan_minutes: int
    projected_credit_cost: Decimal
    policy: SchedulePolicy


def _logical_slack_min(n: _Node) -> int:
    return max(0, int((n.ls - n.es).total_seconds() // 60))


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
    if policy is SchedulePolicy.fastest:
        return lambda x: (
            _logical_slack_min(x),
            0 if (x.ls - x.es).total_seconds() <= 0 else 1,
            x.task.priority,
            x.task.due_date or dt.date.max,
            x.task.created_at,
            str(x.task.id),
        )
    if policy is SchedulePolicy.cheapest:
        return lambda x: (
            1 if rate_of(x) > 0 else 0,
            x.task.priority,
            x.task.due_date or dt.date.max,
            x.task.created_at,
            str(x.task.id),
        )
    if policy is SchedulePolicy.throughput:
        return lambda x: (
            x.duration_min,
            x.task.priority,
            x.task.due_date or dt.date.max,
            x.task.created_at,
            str(x.task.id),
        )
    # balanced: the canonical ADR-0004 priority rule.
    return lambda x: (
        x.task.priority,
        x.task.due_date or dt.date.max,
        x.task.created_at,
        str(x.task.id),
    )


def _effort_minutes(task: Task) -> int:
    hours = task.remaining_effort_h
    if hours is None:
        hours = task.estimate_effort_h
    if hours is None:
        return 0
    return round(float(hours) * 60)


def _manual_pin_start(task: Task, prev: dict[uuid.UUID, Schedule]) -> dt.datetime | None:
    """Pinned start that must survive recompute (schedule_mode=manual,
    previously placed). Single source of truth for both passes."""
    if task.schedule_mode is not ScheduleMode.manual:
        return None
    row = prev.get(task.id)
    return row.scheduled_start if row is not None else None


def _topo(
    ids: list[uuid.UUID],
    deps: list[TaskDependency],
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

    async def recompute(
        self,
        *,
        actor_id: uuid.UUID,
        project_tag_id: uuid.UUID | None = None,
        as_of: dt.datetime | None = None,
        policy: SchedulePolicy = SchedulePolicy.balanced,
    ) -> RecomputeSummary:
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
            )
        ids = [t.id for t in tasks]
        id_set = set(ids)

        deps = [
            d
            for d in (await self._s.execute(select(TaskDependency))).scalars().all()
            if d.predecessor_id in id_set and d.successor_id in id_set
        ]
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
                select(TaskAssignee.task_id, TaskAssignee.user_id).order_by(TaskAssignee.user_id)
            )
        ).all()
        first_assignee: dict[uuid.UUID, uuid.UUID] = {}
        for tid, uid in assignee_rows:
            first_assignee.setdefault(tid, uid)

        prev = {
            row.task_id: row
            for row in (await self._s.execute(select(Schedule))).scalars().all()
            if row.task_id in id_set
        }

        nodes: dict[uuid.UUID, _Node] = {}
        for t in tasks:
            assignee = first_assignee.get(t.id) or t.executor_user_id
            dur = 0 if t.is_milestone else _effort_minutes(t)
            nodes[t.id] = _Node(
                task=t,
                assignee=assignee,
                duration_min=dur,
                terminal=t.state_id in terminal_state_ids,
            )

        incoming: dict[uuid.UUID, list[TaskDependency]] = {i: [] for i in ids}
        outgoing: dict[uuid.UUID, list[TaskDependency]] = {i: [] for i in ids}
        for d in deps:
            incoming[d.successor_id].append(d)
            outgoing[d.predecessor_id].append(d)

        order = _topo(ids, deps)

        # Forward pass: ES/EF.
        for nid in order:
            n = nodes[nid]
            cal, cap = await self._calendar(n.assignee)
            if n.terminal:
                anchor = n.task.actual_start or now
                n.es = anchor
                n.ef = anchor
                continue
            start_lb = [now]
            finish_lb: list[dt.datetime] = []
            if n.task.actual_start is not None:
                start_lb.append(n.task.actual_start)
            cd = n.task.constraint_date
            ck = n.task.constraint_kind.value
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
                es = max(es, cal.add(need, -n.duration_min))
            if cd is not None and ck == "MSO":
                es = cd
            pin = _manual_pin_start(n.task, prev)
            if pin is not None:
                es = pin
            if cd is not None and ck == "MFO":
                n.ef = cd
                n.es = cal.add(cd, -n.duration_min)
            else:
                n.es = es
                n.ef = es if n.duration_min == 0 else cal.add_capped(es, n.duration_min, cap)

        project_end = max(n.ef for n in nodes.values())

        # Backward pass: LF/LS, slack, logical critical path.
        for nid in reversed(order):
            n = nodes[nid]
            cal, _ = await self._calendar(n.assignee)
            finish_ub = [project_end]
            for d in outgoing[nid]:
                snode = nodes[d.successor_id]
                lag = d.lag_working_minutes
                if d.type is DependencyType.FS:
                    finish_ub.append(cal.add(snode.ls, -lag))
                elif d.type is DependencyType.SS:
                    finish_ub.append(cal.add(cal.add(snode.ls, -lag), n.duration_min))
                elif d.type is DependencyType.FF:
                    finish_ub.append(cal.add(snode.lf, -lag))
                else:  # SF
                    finish_ub.append(cal.add(cal.add(snode.ls, -lag), n.duration_min))
            n.lf = min(finish_ub)
            n.ls = cal.add(n.lf, -n.duration_min)

        # --- Stage 2: resource-constrained list scheduling (ADR-0025) ---
        # Executors were just ensured. Index humans by user (the row
        # carries only the switch penalty; the calendar is still
        # resolved by user via build_work_calendar) and pick the single
        # default enabled llm_agent pool (P1: one pool; capability
        # routing across agents is P2).
        exec_rows = list(
            (
                await self._s.execute(
                    select(Executor).order_by(Executor.kind, Executor.name, Executor.id)
                )
            )
            .scalars()
            .all()
        )
        human_exec: dict[uuid.UUID, Executor] = {}
        for e in exec_rows:
            if e.kind is ExecutorKind.human and e.user_id is not None:
                human_exec.setdefault(e.user_id, e)
        agent = next(
            (e for e in exec_rows if e.kind is ExecutorKind.llm_agent and e.enabled),
            None,
        )
        agent_rate = agent.credit_rate_per_hour if agent is not None else Decimal(0)
        agent_max_parallel = agent.max_parallel if agent is not None else 4
        agent_budget = agent.credit_budget if agent is not None else None

        def _effort_hours(n: _Node) -> Decimal:
            h = n.task.remaining_effort_h
            if h is None:
                h = n.task.estimate_effort_h
            return Decimal(0) if h is None else Decimal(h)

        def _node_rate(n: _Node) -> Decimal:
            # Cost is only projected for llm_agent work (the assigned
            # default agent's rate); human work is rate 0.
            return agent_rate if n.task.executor_kind is ExecKind.llm_agent else Decimal(0)

        key = _policy_key(policy, _node_rate)

        # Partition: human serial timeline vs LLM K-pool vs the
        # off-timeline rest (terminals, zero-duration milestones,
        # human tasks with no assignee) which keep their CPM ES/EF.
        sched: dict[uuid.UUID, tuple[dt.datetime, dt.datetime]] = {}
        # Per-resource successor edges built as we place, for the
        # leveled critical-chain backward pass (the task placed right
        # after this one on the same human / freed pool slot).
        res_succ: dict[uuid.UUID, set[uuid.UUID]] = {i: set() for i in ids}
        by_person: dict[uuid.UUID, list[_Node]] = {}
        llm_nodes: list[_Node] = []
        for n in nodes.values():
            if (
                n.task.executor_kind is ExecKind.human
                and n.assignee is not None
                and not n.terminal
                and n.duration_min > 0
            ):
                by_person.setdefault(n.assignee, []).append(n)
            elif (
                n.task.executor_kind is ExecKind.llm_agent and not n.terminal and n.duration_min > 0
            ):
                llm_nodes.append(n)
            else:
                sched[n.task.id] = (n.es, n.ef)

        # Humans: per-person serialization + event-clash avoidance +
        # the policy priority rule. Between consecutive distinct tasks
        # for the person, advance the cursor by that human executor's
        # context_switch_cost_minutes (snap to the calendar). No penalty
        # before the first task and none for a manual pin (a pin is
        # fixed and does not move the switching cursor).
        for user_id, plist in sorted(by_person.items(), key=lambda kv: str(kv[0])):
            cal, cap = await self._calendar(user_id)
            switch_min = (
                human_exec[user_id].context_switch_cost_minutes if user_id in human_exec else 0
            )
            busy = [
                (e.start_at, e.end_at)
                for e in (
                    await self._s.execute(
                        select(Event)
                        .join(
                            EventParticipant,
                            EventParticipant.event_id == Event.id,
                        )
                        .where(EventParticipant.user_id == user_id)
                    )
                )
                .scalars()
                .all()
            ]
            busy.sort()
            plist.sort(key=key)
            cursor = now
            placed_first = False
            prev_node_id: uuid.UUID | None = None
            for n in plist:
                pin = _manual_pin_start(n.task, prev)
                if pin is not None:
                    end = cal.add_capped(pin, n.duration_min, cap)
                    sched[n.task.id] = (pin, end)
                    n.ss, n.se = pin, end
                    # A pin does not advance the switching cursor and
                    # creates no resource-successor edge (it is fixed).
                    continue
                base = max(n.es, cursor)
                if placed_first and switch_min > 0:
                    # Context switch: lose switch_min of working time
                    # before the next distinct task can start.
                    base = cal.add(cal.snap_forward(base), switch_min)
                start = cal.snap_forward(base)
                end = start
                for _ in range(0, 1000):
                    end = cal.add_capped(start, n.duration_min, cap)
                    clash = next(
                        (b for b in busy if b[0] < end and b[1] > start),
                        None,
                    )
                    if clash is None:
                        break
                    start = cal.snap_forward(clash[1])
                sched[n.task.id] = (start, end)
                n.ss, n.se = start, end
                if prev_node_id is not None:
                    res_succ[prev_node_id].add(n.task.id)
                cursor = end
                placed_first = True
                prev_node_id = n.task.id

        # LLM agents: a single K-parallel pool (per the default agent's
        # max_parallel), 24/7 (no working calendar, no daily cap). A
        # task starts at max(its precedence-driven earliest, the
        # earliest freed pool slot). Process in topo order so a
        # predecessor's leveled finish gates its successor even when the
        # pool delayed the predecessor. Cost is projected as placed; a
        # task that would exceed the agent's credit_budget is still
        # scheduled and flagged (budget admission is P2 -- P1 never
        # silently drops). Deterministic: policy key, id final tie-break.
        llm_by_id = {n.task.id: n for n in llm_nodes}
        n_slots = max(1, agent_max_parallel)
        pool_free: list[dt.datetime] = [now] * n_slots
        pool_last: list[uuid.UUID | None] = [None] * n_slots
        cumulative_cost = Decimal(0)
        budget_exceeded = False
        for nid in order:
            ln = llm_by_id.get(nid)
            if ln is None:
                continue
            pin = _manual_pin_start(ln.task, prev)
            if pin is not None:
                end = pin + dt.timedelta(minutes=ln.duration_min)
                sched[ln.task.id] = (pin, end)
                ln.ss, ln.se = pin, end
                cumulative_cost += _effort_hours(ln) * agent_rate
                continue
            earliest = max(ln.es, now)
            for d in incoming[nid]:
                p = nodes[d.predecessor_id]
                if p.se != dt.datetime.min and d.type in (
                    DependencyType.FS,
                    DependencyType.FF,
                ):
                    earliest = max(earliest, p.se)
                elif p.ss != dt.datetime.min:
                    earliest = max(earliest, p.ss)
            # Earliest freed slot (deterministic: smallest free time,
            # ties broken by the lowest slot index).
            slot = min(range(n_slots), key=lambda i: (pool_free[i], i))
            prev_in_slot = pool_last[slot]
            start = max(earliest, pool_free[slot])
            end = start + dt.timedelta(minutes=ln.duration_min)
            pool_free[slot] = end
            pool_last[slot] = nid
            sched[ln.task.id] = (start, end)
            ln.ss, ln.se = start, end
            if prev_in_slot is not None:
                res_succ[prev_in_slot].add(nid)
            cost = _effort_hours(ln) * agent_rate
            cumulative_cost += cost
            if agent_budget is not None and cumulative_cost > agent_budget:
                budget_exceeded = True

        # Resource-aware critical chain (ADR-0025). Definition: a task
        # is on the critical chain iff it has ZERO float in the LEVELED
        # plan -- i.e. delaying it delays the project makespan once
        # resource contention is accounted for. Computed by a backward
        # pass over the leveled graph = precedence successors UNION the
        # per-resource successor (next task on the same human / the same
        # freed pool slot). leveled_lf(n) = makespan if n has no leveled
        # successor else min(leveled_start(succ)); n is on the chain iff
        # leveled_lf(n) - scheduled_end(n) <= 0. This is distinct from
        # on_logical_critical_path (infinite-resource CPM) and is a
        # superset of it under resource contention. Fully deterministic.
        for nid in ids:
            n = nodes[nid]
            if nid not in sched:
                sched[nid] = (n.es, n.ef)
            n.ss, n.se = sched[nid]
        makespan_end = max((n.se for n in nodes.values()), default=now)
        leveled_succ: dict[uuid.UUID, set[uuid.UUID]] = {i: set(res_succ[i]) for i in ids}
        for d in deps:
            leveled_succ[d.predecessor_id].add(d.successor_id)
        for nid in reversed(order):
            n = nodes[nid]
            succs = leveled_succ[nid]
            n.llf = makespan_end if not succs else min(nodes[s].ss for s in succs)
        on_chain: dict[uuid.UUID, bool] = {
            nid: int((nodes[nid].llf - nodes[nid].se).total_seconds()) <= 0 for nid in ids
        }

        await self._s.execute(delete(Schedule).where(Schedule.task_id.in_(ids)))
        fp = hashlib.sha256(
            (
                f"{policy.value}|"
                + "|".join(f"{t.id}:{t.version}" for t in sorted(tasks, key=lambda x: str(x.id)))
            ).encode()
        ).hexdigest()
        total_cost = Decimal(0)
        for nid, n in nodes.items():
            ss, se = sched[nid]
            pcost = (
                _effort_hours(n) * agent_rate
                if n.task.executor_kind is ExecKind.llm_agent
                else Decimal(0)
            )
            total_cost += pcost
            self._s.add(
                Schedule(
                    task_id=nid,
                    org_id=self._org,
                    es=n.es,
                    ef=n.ef,
                    ls=n.ls,
                    lf=n.lf,
                    slack_minutes=max(
                        0,
                        int((n.ls - n.es).total_seconds() // 60),
                    ),
                    on_logical_critical_path=(int((n.ls - n.es).total_seconds()) <= 0),
                    on_critical_chain=on_chain[nid],
                    projected_cost=pcost,
                    scheduled_start=ss,
                    scheduled_end=se,
                    computed_at=now,
                    input_fingerprint=fp,
                )
            )
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
        # budget_exceeded is surfaced via audit detail only in P1
        # (admission/enforcement is P2); the projection itself is the
        # reported signal. Keep the flag referenced to avoid dead code.
        if budget_exceeded:
            await audit.log(
                self._s,
                org_id=self._org,
                actor_id=actor_id,
                entity="schedule",
                entity_id=None,
                action="budget_projection_exceeded",
            )
        return RecomputeSummary(
            count=len(tasks),
            makespan_minutes=makespan_minutes,
            projected_credit_cost=total_cost,
            policy=policy,
        )


async def recompute(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_tag_id: uuid.UUID | None = None,
    as_of: dt.datetime | None = None,
    policy: SchedulePolicy = SchedulePolicy.balanced,
) -> RecomputeSummary:
    return await Scheduler(session, org_id).recompute(
        actor_id=actor_id,
        project_tag_id=project_tag_id,
        as_of=as_of,
        policy=policy,
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
