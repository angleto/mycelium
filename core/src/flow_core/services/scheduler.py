"""Deterministic scheduler (docs/adr/0004, FR-4).

Logical CPM forward/backward pass over working calendars + per-person
serialization of human, non-delegated tasks around fixed events
(no-ubiquity). LLM-executor tasks are parallel (off the human
timeline). Manual/pinned and in-progress tasks survive recompute.
Deterministic: stable ordering and tie-breaks throughout. NOT RCPSP.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.errors import DomainError
from flow_core.i18n import MessageCode
from flow_core.models.dependency import DependencyType, TaskDependency
from flow_core.models.event import Event, EventParticipant
from flow_core.models.membership import Role
from flow_core.models.schedule import Schedule
from flow_core.models.task import ExecKind, ScheduleMode, Task
from flow_core.models.task_assignee import TaskAssignee
from flow_core.models.task_tag import TaskTag
from flow_core.models.workflow import WorkflowState
from flow_core.services import audit
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
    ) -> int:
        await require_role(self._s, self._org, actor_id, Role.member)
        now = (as_of or dt.datetime.now(tz=dt.UTC)).astimezone(dt.UTC)

        stmt = select(Task).where(Task.deleted_at.is_(None), Task.is_archived.is_(False))
        if project_tag_id is not None:
            stmt = stmt.join(TaskTag, TaskTag.task_id == Task.id).where(
                TaskTag.tag_id == project_tag_id
            )
        tasks = list((await self._s.execute(stmt)).scalars().unique().all())
        if not tasks:
            return 0
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

        # Per-person serialization of human, non-delegated tasks
        # around fixed events. LLM tasks are parallel (ES/EF).
        sched: dict[uuid.UUID, tuple[dt.datetime, dt.datetime]] = {}
        by_person: dict[uuid.UUID, list[_Node]] = {}
        for n in nodes.values():
            if (
                n.task.executor_kind is ExecKind.human
                and n.assignee is not None
                and not n.terminal
                and n.duration_min > 0
            ):
                by_person.setdefault(n.assignee, []).append(n)
            else:
                sched[n.task.id] = (n.es, n.ef)

        for user_id, plist in by_person.items():
            cal, cap = await self._calendar(user_id)
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
            # Deterministic priority rule (docs/adr/0004): most important
            # first. Priority is a four-level P1..P4 where P1 (= 1) is the
            # highest, so order by priority ascending; then earliest due,
            # earliest created, and id as the final stable tie-break.
            plist.sort(
                key=lambda x: (
                    x.task.priority,
                    x.task.due_date or dt.date.max,
                    x.task.created_at,
                    str(x.task.id),
                )
            )
            cursor = now
            for n in plist:
                pin = _manual_pin_start(n.task, prev)
                if pin is not None:
                    sched[n.task.id] = (pin, cal.add_capped(pin, n.duration_min, cap))
                    continue
                start = cal.snap_forward(max(n.es, cursor))
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
                cursor = end

        await self._s.execute(delete(Schedule).where(Schedule.task_id.in_(ids)))
        fp = hashlib.sha256(
            "|".join(f"{t.id}:{t.version}" for t in sorted(tasks, key=lambda x: str(x.id))).encode()
        ).hexdigest()
        for nid, n in nodes.items():
            ss, se = sched[nid]
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
        return len(tasks)


async def recompute(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    project_tag_id: uuid.UUID | None = None,
    as_of: dt.datetime | None = None,
) -> int:
    return await Scheduler(session, org_id).recompute(
        actor_id=actor_id, project_tag_id=project_tag_id, as_of=as_of
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
