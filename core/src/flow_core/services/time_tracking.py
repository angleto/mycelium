"""Time tracking service (docs/adr/0002, FR-5).

A live timer is a ``time_entries`` row with ``ended_at IS NULL``. The
single-running-timer invariant is enforced by a partial unique index
(migration 0006), not only here. The billing rate is snapshotted from
the task's project profile at creation, so later rate edits never
rewrite history. The first activity on a task sets ``task.actual_start``
(set-once, earliest-wins), feeding the deterministic scheduler residual
(FR-4): it is system-derived, written outside optimistic concurrency
like the derived schedule, so it never raises spurious 409s on user
edits.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from flow_core.concurrency import optimistic_update
from flow_core.errors import DomainError, NotFoundError
from flow_core.i18n import MessageCode
from flow_core.models.membership import Role
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import ExecKind, Task
from flow_core.models.task_tag import TaskTag
from flow_core.models.time_entry import TimeEntry, TimeSource
from flow_core.models.user import User
from flow_core.services import audit
from flow_core.services.rbac import require_role
from flow_core.services.tasks import get_task

_UPDATABLE = frozenset({"note", "billable"})


class ReportGroup(enum.StrEnum):
    project = "project"
    client = "client"
    generic = "generic"
    user = "user"
    task = "task"


@dataclass(frozen=True)
class ReportRow:
    key: str | None
    label: str | None
    seconds: int
    billable_seconds: int
    amount: Decimal
    currency: str


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def _rate(session: AsyncSession, task_id: uuid.UUID) -> tuple[Decimal | None, str]:
    """Billing rate + currency snapshot from the task's project tag."""
    project_tag_id = (
        await session.execute(
            select(Tag.id)
            .join(TaskTag, TaskTag.tag_id == Tag.id)
            .where(TaskTag.task_id == task_id, Tag.kind == TagKind.project)
            .order_by(Tag.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if project_tag_id is None:
        return (None, "EUR")
    prof = (
        await session.execute(select(ProjectProfile).where(ProjectProfile.tag_id == project_tag_id))
    ).scalar_one_or_none()
    if prof is None:
        return (None, "EUR")
    return (prof.tariffa, prof.valuta)


async def _touch_actual_start(session: AsyncSession, task_id: uuid.UUID, ts: dt.datetime) -> None:
    """Earliest-wins, set-once derived marker (no version bump)."""
    await session.execute(
        update(Task)
        .where(
            Task.id == task_id,
            (Task.actual_start.is_(None)) | (Task.actual_start > ts),
        )
        .values(actual_start=ts)
    )


async def get_entry(session: AsyncSession, *, org_id: uuid.UUID, entry_id: uuid.UUID) -> TimeEntry:
    e = (
        await session.execute(select(TimeEntry).where(TimeEntry.id == entry_id))
    ).scalar_one_or_none()
    if e is None:
        raise NotFoundError(MessageCode.TIME_ENTRY_NOT_FOUND)
    return e


async def running_entry(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> TimeEntry | None:
    return (
        await session.execute(
            select(TimeEntry).where(TimeEntry.user_id == user_id, TimeEntry.ended_at.is_(None))
        )
    ).scalar_one_or_none()


async def start_timer(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    billable: bool = True,
    note: str | None = None,
) -> TimeEntry:
    await require_role(session, org_id, actor_id, Role.member)
    task = await get_task(session, org_id=org_id, task_id=task_id)
    rate, currency = await _rate(session, task_id)
    started = _now()
    entry = TimeEntry(
        org_id=org_id,
        task_id=task_id,
        user_id=actor_id,
        started_at=started,
        ended_at=None,
        duration_seconds=None,
        source=TimeSource.timer,
        executor_kind=task.executor_kind,
        billable=billable,
        rate_snapshot=rate,
        currency=currency,
        note=note,
    )
    try:
        async with session.begin_nested():
            session.add(entry)
            await session.flush()
    except IntegrityError as exc:
        raise DomainError(MessageCode.TIMER_ALREADY_RUNNING) from exc
    await _touch_actual_start(session, task_id, started)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="time_entry",
        entity_id=entry.id,
        action="start",
    )
    return entry


async def stop_timer(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    note: str | None = None,
) -> TimeEntry:
    await require_role(session, org_id, actor_id, Role.member)
    entry = await running_entry(session, org_id=org_id, user_id=actor_id)
    if entry is None:
        raise DomainError(MessageCode.NO_RUNNING_TIMER)
    ended = _now()
    values: dict[str, Any] = {
        "ended_at": ended,
        "duration_seconds": int((ended - entry.started_at).total_seconds()),
    }
    if note is not None:
        values["note"] = note
    await optimistic_update(
        session,
        TimeEntry,
        pk=entry.id,
        expected_version=entry.version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="time_entry",
        entity_id=entry.id,
        action="stop",
    )
    return await get_entry(session, org_id=org_id, entry_id=entry.id)


async def add_manual_entry(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID,
    started_at: dt.datetime,
    ended_at: dt.datetime | None = None,
    duration_seconds: int | None = None,
    billable: bool = True,
    note: str | None = None,
) -> TimeEntry:
    await require_role(session, org_id, actor_id, Role.member)
    task = await get_task(session, org_id=org_id, task_id=task_id)
    if ended_at is not None:
        if ended_at <= started_at:
            raise DomainError(MessageCode.TIME_ENTRY_INVALID)
        seconds = int((ended_at - started_at).total_seconds())
    elif duration_seconds is not None and duration_seconds > 0:
        seconds = duration_seconds
        ended_at = started_at + dt.timedelta(seconds=seconds)
    else:
        raise DomainError(MessageCode.TIME_ENTRY_INVALID)
    rate, currency = await _rate(session, task_id)
    entry = TimeEntry(
        org_id=org_id,
        task_id=task_id,
        user_id=actor_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=seconds,
        source=TimeSource.manual,
        executor_kind=task.executor_kind,
        billable=billable,
        rate_snapshot=rate,
        currency=currency,
        note=note,
    )
    session.add(entry)
    await session.flush()
    await _touch_actual_start(session, task_id, started_at)
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="time_entry",
        entity_id=entry.id,
        action="manual",
    )
    return entry


async def list_entries(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    start_from: dt.datetime | None = None,
    start_to: dt.datetime | None = None,
    billable: bool | None = None,
) -> list[TimeEntry]:
    stmt = select(TimeEntry)
    if task_id is not None:
        stmt = stmt.where(TimeEntry.task_id == task_id)
    if user_id is not None:
        stmt = stmt.where(TimeEntry.user_id == user_id)
    if start_from is not None:
        stmt = stmt.where(TimeEntry.started_at >= start_from)
    if start_to is not None:
        stmt = stmt.where(TimeEntry.started_at < start_to)
    if billable is not None:
        stmt = stmt.where(TimeEntry.billable.is_(billable))
    stmt = stmt.order_by(TimeEntry.started_at)
    return list((await session.execute(stmt)).scalars().all())


async def update_entry(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entry_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> int:
    if not values or set(values) - _UPDATABLE:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    await get_entry(session, org_id=org_id, entry_id=entry_id)
    new_version = await optimistic_update(
        session,
        TimeEntry,
        pk=entry_id,
        expected_version=expected_version,
        values=values,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="time_entry",
        entity_id=entry_id,
        action="update",
        diff={k: str(v) for k, v in values.items()},
    )
    return new_version


async def delete_entry(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entry_id: uuid.UUID,
) -> None:
    await require_role(session, org_id, actor_id, Role.member)
    e = await get_entry(session, org_id=org_id, entry_id=entry_id)
    await session.delete(e)
    await session.flush()
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="time_entry",
        entity_id=entry_id,
        action="delete",
    )


def _amount(seconds: int, rate: Decimal | None) -> Decimal:
    if rate is None:
        return Decimal(0)
    return (Decimal(seconds) / Decimal(3600) * rate).quantize(Decimal("0.01"))


async def _group_keys(
    session: AsyncSession,
    task_ids: Sequence[uuid.UUID],
    kind: TagKind,
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]]]:
    if not task_ids:
        return {}
    rows = (
        await session.execute(
            select(TaskTag.task_id, Tag.id, Tag.name)
            .join(Tag, Tag.id == TaskTag.tag_id)
            .where(TaskTag.task_id.in_(task_ids), Tag.kind == kind)
            .order_by(Tag.id)
        )
    ).all()
    out: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    for task_id, tag_id, name in rows:
        out.setdefault(task_id, []).append((tag_id, name))
    return out


async def report(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    group_by: ReportGroup,
    start_from: dt.datetime | None = None,
    start_to: dt.datetime | None = None,
    billable: bool | None = None,
    executor_kind: ExecKind | None = None,
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
) -> list[ReportRow]:
    """Aggregate completed entries (running timers excluded). Amount
    sums only billable entries that carry a rate snapshot. ``client_tag_id``
    / ``project_tag_id`` scope the report to entries whose task carries the
    given client / project tag."""
    await require_role(session, org_id, actor_id, Role.member)
    entries = await list_entries(
        session,
        org_id=org_id,
        start_from=start_from,
        start_to=start_to,
        billable=billable,
    )
    entries = [e for e in entries if e.duration_seconds is not None]
    if executor_kind is not None:
        entries = [e for e in entries if e.executor_kind is executor_kind]
    for tag_id in (client_tag_id, project_tag_id):
        if tag_id is None:
            continue
        task_ids = [e.task_id for e in entries]
        if not task_ids:
            break
        keep = {
            tid
            for (tid,) in (
                await session.execute(
                    select(TaskTag.task_id).where(
                        TaskTag.task_id.in_(task_ids), TaskTag.tag_id == tag_id
                    )
                )
            ).all()
        }
        entries = [e for e in entries if e.task_id in keep]

    # acc key -> [label, seconds, billable_seconds, amount, currency]
    acc: dict[str | None, list[Any]] = {}

    def bump(
        key: str | None,
        label: str | None,
        seconds: int,
        is_billable: bool,
        rate: Decimal | None,
        currency: str,
    ) -> None:
        slot = acc.setdefault(key, [label, 0, 0, Decimal(0), currency])
        slot[1] += seconds
        if is_billable:
            slot[2] += seconds
            slot[3] += _amount(seconds, rate)

    if group_by in (ReportGroup.user, ReportGroup.task):
        is_user = group_by is ReportGroup.user
        labels: dict[uuid.UUID, str] = {}
        if is_user:
            ids = {e.user_id for e in entries}
            if ids:
                labels = {
                    uid: email
                    for uid, email in (
                        await session.execute(select(User.id, User.email).where(User.id.in_(ids)))
                    ).all()
                }
        else:
            ids = {e.task_id for e in entries}
            if ids:
                labels = {
                    tid: title
                    for tid, title in (
                        await session.execute(select(Task.id, Task.title).where(Task.id.in_(ids)))
                    ).all()
                }
        for e in entries:
            ent_id = e.user_id if is_user else e.task_id
            bump(
                str(ent_id),
                labels.get(ent_id),
                e.duration_seconds or 0,
                e.billable,
                e.rate_snapshot,
                e.currency,
            )
    else:
        kind = {
            ReportGroup.project: TagKind.project,
            ReportGroup.client: TagKind.client,
            ReportGroup.generic: TagKind.generic,
        }[group_by]
        tag_map = await _group_keys(session, [e.task_id for e in entries], kind)
        for e in entries:
            tags = tag_map.get(e.task_id, [])
            if not tags:
                bump(None, None, e.duration_seconds or 0, e.billable, e.rate_snapshot, e.currency)
                continue
            for tag_id, name in tags:
                bump(
                    str(tag_id),
                    name,
                    e.duration_seconds or 0,
                    e.billable,
                    e.rate_snapshot,
                    e.currency,
                )

    rows = [
        ReportRow(
            key=k,
            label=v[0],
            seconds=v[1],
            billable_seconds=v[2],
            amount=v[3],
            currency=v[4],
        )
        for k, v in acc.items()
    ]
    rows.sort(key=lambda r: (r.label or "", r.key or ""))
    return rows
