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
from flow_core.models.client_profile import ClientProfile
from flow_core.models.membership import Role
from flow_core.models.note import Note
from flow_core.models.project_profile import ProjectProfile
from flow_core.models.tag import Tag, TagKind
from flow_core.models.task import ExecKind, Task
from flow_core.models.task_tag import TaskTag
from flow_core.models.time_entry import TimeEntry, TimeSource
from flow_core.models.user import User
from flow_core.services import audit
from flow_core.services import note_links as note_links_svc
from flow_core.services.rbac import require_role
from flow_core.services.tasks import get_task

_UPDATABLE = frozenset({"memo", "billable", "task_id", "started_at", "ended_at", "note_id"})


async def _task_id_for_note(
    session: AsyncSession, *, org_id: uuid.UUID, note_id: uuid.UUID
) -> uuid.UUID:
    """Proposal A: a note is the work log of exactly one task. Load the
    note RLS-scoped in-org (NOTE_NOT_FOUND if absent / cross-org). The
    note MUST already be linked to a task, else NOTE_NOT_LINKED_TO_TASK
    (you cannot bill time to a note that has no task to roll up to).

    docs/adr/0029 P3: the link comes through the typed
    ``note_task_link`` (resolved via ``primary_task_id_for_note``)
    instead of the legacy ``Note.task_id`` column.
    """
    note = (
        await session.execute(select(Note).where(Note.id == note_id, Note.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if note is None:
        raise NotFoundError(MessageCode.NOTE_NOT_FOUND)
    task_id = await note_links_svc.primary_task_id_for_note(session, org_id=org_id, note_id=note_id)
    if task_id is None:
        raise DomainError(MessageCode.NOTE_NOT_LINKED_TO_TASK)
    return task_id


async def _resolve_billing_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    task_id: uuid.UUID | None,
    note_id: uuid.UUID | None,
) -> uuid.UUID:
    """The billing task for a new/edited entry. With a ``note_id`` the
    task is derived from ``note.task_id`` (Proposal A); if ``task_id``
    is also given the two MUST agree (DOMAIN_ERROR otherwise). Without a
    note the explicit ``task_id`` is used as-is. The caller still
    validates task existence (``get_task``) for the no-note path."""
    if note_id is not None:
        note_task = await _task_id_for_note(session, org_id=org_id, note_id=note_id)
        if task_id is not None and task_id != note_task:
            raise DomainError(MessageCode.DOMAIN_ERROR)
        return note_task
    if task_id is None:
        # No note to derive a billing task from and no explicit task:
        # there is nothing to roll the time up to.
        raise DomainError(MessageCode.TIME_ENTRY_INVALID)
    return task_id


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


async def _rate(session: AsyncSession, task_id: uuid.UUID) -> tuple[Decimal | None, str, bool]:
    """Rate + currency (from the task's project) + the billable default
    (from that project's CLIENT — billing is a client relationship).
    Defaults (no project / no client): no rate, EUR, billable."""
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
        return (None, "EUR", True)
    prof = (
        await session.execute(select(ProjectProfile).where(ProjectProfile.tag_id == project_tag_id))
    ).scalar_one_or_none()
    if prof is None or prof.client_tag_id is None:
        return (None, "EUR", True)
    # Rate, currency AND billable default all live on the client.
    cp = (
        await session.execute(
            select(ClientProfile).where(ClientProfile.tag_id == prof.client_tag_id)
        )
    ).scalar_one_or_none()
    if cp is None:
        return (None, "EUR", True)
    return (cp.hourly_rate, cp.currency, cp.default_billable)


def _effective_billable(explicit: bool | None, task: Task, client_default: bool) -> bool:
    """Explicit arg wins; else the task override; else the client's
    default_billable (true with no client)."""
    if explicit is not None:
        return explicit
    if task.billable is not None:
        return task.billable
    return client_default


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


async def running_entries(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[TimeEntry]:
    """All live timers for the user (serial + any parallel ones)."""
    return list(
        (
            await session.execute(
                select(TimeEntry)
                .where(TimeEntry.user_id == user_id, TimeEntry.ended_at.is_(None))
                .order_by(TimeEntry.started_at)
            )
        )
        .scalars()
        .all()
    )


async def running_serial(
    session: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> TimeEntry | None:
    """The single mutually-exclusive timer (parallel = false), if any."""
    return (
        await session.execute(
            select(TimeEntry).where(
                TimeEntry.user_id == user_id,
                TimeEntry.ended_at.is_(None),
                TimeEntry.parallel.is_(False),
            )
        )
    ).scalar_one_or_none()


async def running_for_task(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
) -> TimeEntry | None:
    return (
        await session.execute(
            select(TimeEntry).where(
                TimeEntry.user_id == user_id,
                TimeEntry.task_id == task_id,
                TimeEntry.ended_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _stop_entry(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    entry: TimeEntry,
    memo: str | None = None,
) -> TimeEntry:
    ended = _now()
    values: dict[str, Any] = {
        "ended_at": ended,
        "duration_seconds": int((ended - entry.started_at).total_seconds()),
    }
    if memo is not None:
        values["memo"] = memo
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


async def start_timer(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    billable: bool | None = None,
    memo: str | None = None,
    note_id: uuid.UUID | None = None,
    parallel: bool = False,
) -> TimeEntry:
    """Serial (default): one running timer at a time; starting it stops
    the previous serial one. Parallel: runs alongside others (e.g. LLM
    tasks), stops nothing. The same task is never double-tracked (DB
    partial unique index -> TIMER_ALREADY_RUNNING).

    Proposal A: ``note_id`` (optional) records *in which work note* the
    time was logged. The note must be linked to a task; the billing
    task is derived from it (``task_id`` may be omitted, or must agree
    if both are given). The entry stores both ``task_id`` (billing
    rollup, NOT NULL) and ``note_id`` (provenance)."""
    await require_role(session, org_id, actor_id, Role.member)
    task_id = await _resolve_billing_task(session, org_id=org_id, task_id=task_id, note_id=note_id)
    task = await get_task(session, org_id=org_id, task_id=task_id)
    if not parallel:
        current = await running_serial(session, org_id=org_id, user_id=actor_id)
        if current is not None:
            await _stop_entry(session, org_id=org_id, actor_id=actor_id, entry=current)
    rate, currency, client_billable = await _rate(session, task_id)
    eff_billable = _effective_billable(billable, task, client_billable)
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
        billable=eff_billable,
        rate_snapshot=rate,
        currency=currency,
        memo=memo,
        note_id=note_id,
        parallel=parallel,
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
    task_id: uuid.UUID | None = None,
    memo: str | None = None,
) -> TimeEntry:
    """Stop the running timer for ``task_id`` (a specific row), or the
    serial timer when ``task_id`` is omitted."""
    await require_role(session, org_id, actor_id, Role.member)
    if task_id is not None:
        entry = await running_for_task(session, org_id=org_id, user_id=actor_id, task_id=task_id)
    else:
        entry = await running_serial(session, org_id=org_id, user_id=actor_id)
    if entry is None:
        raise DomainError(MessageCode.NO_RUNNING_TIMER)
    return await _stop_entry(session, org_id=org_id, actor_id=actor_id, entry=entry, memo=memo)


async def add_manual_entry(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    task_id: uuid.UUID | None = None,
    started_at: dt.datetime,
    ended_at: dt.datetime | None = None,
    duration_seconds: int | None = None,
    billable: bool | None = None,
    memo: str | None = None,
    note_id: uuid.UUID | None = None,
) -> TimeEntry:
    await require_role(session, org_id, actor_id, Role.member)
    task_id = await _resolve_billing_task(session, org_id=org_id, task_id=task_id, note_id=note_id)
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
    rate, currency, client_billable = await _rate(session, task_id)
    eff_billable = _effective_billable(billable, task, client_billable)
    entry = TimeEntry(
        org_id=org_id,
        task_id=task_id,
        user_id=actor_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=seconds,
        source=TimeSource.manual,
        executor_kind=task.executor_kind,
        billable=eff_billable,
        rate_snapshot=rate,
        currency=currency,
        memo=memo,
        note_id=note_id,
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
    client_tag_id: uuid.UUID | None = None,
    project_tag_id: uuid.UUID | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[TimeEntry]:
    """Newest first. ``limit`` paginates the recency list; the report
    aggregation calls without a limit (order is irrelevant there).

    ``client_tag_id`` / ``project_tag_id`` scope the list to entries
    whose task carries the given client / project tag — same semantics
    as the report filters, applied at the database level so pagination
    counts the filtered rows rather than the unfiltered window."""
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
    for tag_id in (client_tag_id, project_tag_id):
        if tag_id is None:
            continue
        stmt = stmt.where(
            TimeEntry.task_id.in_(select(TaskTag.task_id).where(TaskTag.tag_id == tag_id))
        )
    stmt = stmt.order_by(TimeEntry.started_at.desc(), TimeEntry.id.desc())
    if limit is not None:
        stmt = stmt.offset(offset).limit(limit)
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
    """Correct an entry. Beyond memo/billable a user can:

    - ``task_id``: reassign the entry to another task (transitively
      changing its project/client). The new task must exist in the
      tenant, else NotFoundError TASK_NOT_FOUND.
    - ``note_id``: set / clear (explicit ``None``) the work note this
      time was logged in. Preserved when the key is absent. When set,
      the same Proposal A consistency rule as start/create applies: the
      note must exist in-org (NOTE_NOT_FOUND) and be linked to a task,
      and that task must agree with the entry's (final) billing task,
      else DOMAIN_ERROR.
    - ``started_at`` / ``ended_at``: adjust the interval if the timer
      was started late / never stopped. The final interval must be
      open-ended (still running) or have ``ended_at > started_at``,
      else DomainError TIME_ENTRY_INVALID. ``duration_seconds`` is
      recomputed from the final start/end (None while running).

    One cohesive write under the existing optimistic version guard."""
    if not values or set(values) - _UPDATABLE:
        raise DomainError(MessageCode.DOMAIN_ERROR)
    await require_role(session, org_id, actor_id, Role.member)
    entry = await get_entry(session, org_id=org_id, entry_id=entry_id)

    patch = dict(values)
    touches_interval = "started_at" in patch or "ended_at" in patch
    new_task: Task | None = None
    if "task_id" in patch:
        # Validates same-tenant existence (RLS scopes the lookup);
        # raises NotFoundError(TASK_NOT_FOUND) if absent. Hold on to
        # the resolved Task so we can re-snapshot rate/currency below.
        new_task = await get_task(session, org_id=org_id, task_id=patch["task_id"])
    # Proposal A invariant after the patch: the *effective* note_id
    # (the new one if set, the stored one if untouched, none if cleared)
    # must be linked to the *effective* billing task (the reassigned one
    # if patched, else the stored task_id). This rejects both linking an
    # inconsistent note AND reassigning the task out from under a note
    # that is still attached. Clearing the note (explicit None) needs no
    # check. Note existence/linkage (NOTE_NOT_FOUND /
    # NOTE_NOT_LINKED_TO_TASK) is validated by _task_id_for_note.
    final_task_id = patch.get("task_id", entry.task_id)
    eff_note_id = patch["note_id"] if "note_id" in patch else entry.note_id
    if eff_note_id is not None:
        note_task = await _task_id_for_note(session, org_id=org_id, note_id=eff_note_id)
        if note_task != final_task_id:
            raise DomainError(MessageCode.DOMAIN_ERROR)
    if touches_interval:
        new_started = patch.get("started_at", entry.started_at)
        # ended_at explicitly in the patch wins (including an explicit
        # None to "un-stop"); otherwise the stored value is kept.
        new_ended = patch["ended_at"] if "ended_at" in patch else entry.ended_at
        if new_ended is not None and new_ended <= new_started:
            raise DomainError(MessageCode.TIME_ENTRY_INVALID)
        patch["started_at"] = new_started
        patch["ended_at"] = new_ended
        patch["duration_seconds"] = (
            None if new_ended is None else int((new_ended - new_started).total_seconds())
        )

    # Re-snapshot billing when the entry moves to a different task OR
    # when ``billable`` is explicitly toggled. Moving an entry to a task
    # under a different client must follow the chain (a Kiwi entry
    # reassigned to a non-billable internal task is now non-billable
    # and earns 0 EUR; the reverse mistake also self-corrects). The
    # snapshot is overwritten — this is a correction, not history.
    if new_task is not None or "billable" in patch:
        snapshot_task = new_task or await get_task(session, org_id=org_id, task_id=entry.task_id)
        rate, currency, client_billable = await _rate(session, snapshot_task.id)
        # When the user explicitly toggles billable in this patch, that
        # value is the override; otherwise let _effective_billable walk
        # the chain (task.billable -> client.default_billable). Passing
        # the stored ``entry.billable`` as the explicit would pin the
        # final value and defeat the whole point of re-snapshotting.
        explicit = patch["billable"] if "billable" in patch else None
        patch["rate_snapshot"] = rate
        patch["currency"] = currency
        patch["billable"] = _effective_billable(explicit, snapshot_task, client_billable)

    new_version = await optimistic_update(
        session,
        TimeEntry,
        pk=entry_id,
        expected_version=expected_version,
        values=patch,
    )
    await audit.log(
        session,
        org_id=org_id,
        actor_id=actor_id,
        entity="time_entry",
        entity_id=entry_id,
        action="update",
        diff={k: str(v) for k, v in patch.items()},
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

    # rate_snapshot is set when the entry is created/edited; entries
    # created when the client had no ``hourly_rate`` (or no client) have
    # NULL or ZERO snapshots that historically rendered as 0 EUR even
    # after the rate was later configured. Backfill the missing rates
    # LIVE from the current task -> project -> client lookup, without
    # mutating history: snapshot semantics ("the rate at the time of
    # the entry") only apply when the snapshot is a real positive
    # number. NULL / 0 mean "no rate was captured" (the explicit-free
    # case is rare and indistinguishable from "didn't know yet"; the
    # live rate is the most accurate available value either way).
    live_rates: dict[uuid.UUID, tuple[Decimal | None, str]] = {}
    needing_live = {
        e.task_id
        for e in entries
        if e.billable and (e.rate_snapshot is None or e.rate_snapshot <= 0)
    }
    for tid in needing_live:
        r, cur, _ = await _rate(session, tid)
        live_rates[tid] = (r, cur)

    def rate_for(e: TimeEntry) -> Decimal | None:
        if e.rate_snapshot is not None and e.rate_snapshot > 0:
            return e.rate_snapshot
        return live_rates.get(e.task_id, (None, "EUR"))[0]

    def currency_for(e: TimeEntry) -> str:
        if e.rate_snapshot is not None and e.rate_snapshot > 0:
            return e.currency
        live = live_rates.get(e.task_id)
        return live[1] if live else e.currency

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
                rate_for(e),
                currency_for(e),
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
                bump(None, None, e.duration_seconds or 0, e.billable, rate_for(e), currency_for(e))
                continue
            for tag_id, name in tags:
                # Use the live-rate fallback helpers same as the
                # user/task branch — the project/client/generic
                # aggregation used to read e.rate_snapshot directly,
                # bypassing the fix that backfills NULL / zero
                # snapshots with the current task -> client rate.
                bump(
                    str(tag_id),
                    name,
                    e.duration_seconds or 0,
                    e.billable,
                    rate_for(e),
                    currency_for(e),
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


@dataclass(frozen=True)
class TaskContext:
    """Resolved task -> project tag -> client tag -> client profile
    chain, used to enrich TimeEntryOut and the per-task report without
    per-row queries."""

    task_title: str | None = None
    project_tag_id: uuid.UUID | None = None
    project_name: str | None = None
    client_tag_id: uuid.UUID | None = None
    client_name: str | None = None
    client_timezone: str | None = None


_EMPTY_CONTEXT = TaskContext()


async def resolve_task_contexts(
    session: AsyncSession, task_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, TaskContext]:
    """Batched task -> context resolver: a fixed, small number of
    queries regardless of how many tasks/entries (no N+1). The project
    tag is the earliest-by-id project tag on the task (same selection
    as ``_rate``), then ProjectProfile.client_tag_id, then the client
    tag's name and ClientProfile.timezone."""
    ids = list({t for t in task_ids})
    if not ids:
        return {}
    titles: dict[uuid.UUID, str | None] = {
        tid: title
        for tid, title in (
            await session.execute(select(Task.id, Task.title).where(Task.id.in_(ids)))
        ).all()
    }
    # Earliest project tag per task (Tag.id order mirrors _rate()).
    proj_by_task: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
    for task_id, tag_id, name in (
        await session.execute(
            select(TaskTag.task_id, Tag.id, Tag.name)
            .join(Tag, Tag.id == TaskTag.tag_id)
            .where(TaskTag.task_id.in_(ids), Tag.kind == TagKind.project)
            .order_by(Tag.id)
        )
    ).all():
        proj_by_task.setdefault(task_id, (tag_id, name))

    project_tag_ids = list({p[0] for p in proj_by_task.values()})
    client_by_project: dict[uuid.UUID, uuid.UUID] = {}
    if project_tag_ids:
        for ptag, ctag in (
            await session.execute(
                select(ProjectProfile.tag_id, ProjectProfile.client_tag_id).where(
                    ProjectProfile.tag_id.in_(project_tag_ids),
                    ProjectProfile.client_tag_id.is_not(None),
                )
            )
        ).all():
            client_by_project[ptag] = ctag

    client_tag_ids = list(set(client_by_project.values()))
    client_name: dict[uuid.UUID, str] = {}
    client_tz: dict[uuid.UUID, str | None] = {}
    if client_tag_ids:
        client_name = {
            cid: name
            for cid, name in (
                await session.execute(select(Tag.id, Tag.name).where(Tag.id.in_(client_tag_ids)))
            ).all()
        }
        client_tz = {
            cid: tz
            for cid, tz in (
                await session.execute(
                    select(ClientProfile.tag_id, ClientProfile.timezone).where(
                        ClientProfile.tag_id.in_(client_tag_ids)
                    )
                )
            ).all()
        }

    out: dict[uuid.UUID, TaskContext] = {}
    for tid in ids:
        proj = proj_by_task.get(tid)
        ptag = proj[0] if proj else None
        ctag = client_by_project.get(ptag) if ptag is not None else None
        out[tid] = TaskContext(
            task_title=titles.get(tid),
            project_tag_id=ptag,
            project_name=proj[1] if proj else None,
            client_tag_id=ctag,
            client_name=client_name.get(ctag) if ctag is not None else None,
            client_timezone=client_tz.get(ctag) if ctag is not None else None,
        )
    return out


async def context_for_entry(session: AsyncSession, entry: TimeEntry) -> TaskContext:
    """Single-entry convenience over ``resolve_task_contexts`` (used by
    the start/stop/get/patch endpoints, which return one entry)."""
    ctxs = await resolve_task_contexts(session, [entry.task_id])
    return ctxs.get(entry.task_id, _EMPTY_CONTEXT)


async def resolve_note_titles(
    session: AsyncSession, note_ids: Sequence[uuid.UUID | None]
) -> dict[uuid.UUID, str | None]:
    """Batched note-id -> title for the entries list / report drill-down
    (Proposal A: show *in which work note* time was logged). One query
    regardless of how many entries (no N+1); RLS scopes to the org so a
    cross-org note id simply resolves to no title. Soft-deleted notes
    still resolve their title (the entry's provenance is historical)."""
    ids = list({i for i in note_ids if i is not None})
    if not ids:
        return {}
    return {
        nid: title
        for nid, title in (
            await session.execute(select(Note.id, Note.title).where(Note.id.in_(ids)))
        ).all()
    }


@dataclass(frozen=True)
class TaskTimeReportRow:
    task_id: uuid.UUID
    task_title: str | None
    project_tag_id: uuid.UUID | None
    project_name: str | None
    client_tag_id: uuid.UUID | None
    client_name: str | None
    client_timezone: str | None
    total_seconds: int
    billable_seconds: int
    entry_count: int


async def task_report(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    start_from: dt.datetime | None = None,
    start_to: dt.datetime | None = None,
) -> list[TaskTimeReportRow]:
    """Per-task aggregate over the *acting user's* completed entries
    (running timers excluded, no duration yet). Honours the same
    optional ``start_from``/``start_to`` window as ``list_entries``.
    Ordered by total_seconds desc (ties: task title)."""
    await require_role(session, org_id, actor_id, Role.member)
    entries = await list_entries(
        session,
        org_id=org_id,
        user_id=actor_id,
        start_from=start_from,
        start_to=start_to,
    )
    entries = [e for e in entries if e.duration_seconds is not None]
    ctxs = await resolve_task_contexts(session, [e.task_id for e in entries])
    # task_id -> [total, billable, count]
    acc: dict[uuid.UUID, list[int]] = {}
    for e in entries:
        slot = acc.setdefault(e.task_id, [0, 0, 0])
        secs = e.duration_seconds or 0
        slot[0] += secs
        if e.billable:
            slot[1] += secs
        slot[2] += 1
    rows: list[TaskTimeReportRow] = []
    for task_id, (total, billable_s, count) in acc.items():
        ctx = ctxs.get(task_id, _EMPTY_CONTEXT)
        rows.append(
            TaskTimeReportRow(
                task_id=task_id,
                task_title=ctx.task_title,
                project_tag_id=ctx.project_tag_id,
                project_name=ctx.project_name,
                client_tag_id=ctx.client_tag_id,
                client_name=ctx.client_name,
                client_timezone=ctx.client_timezone,
                total_seconds=total,
                billable_seconds=billable_s,
                entry_count=count,
            )
        )
    rows.sort(key=lambda r: (-r.total_seconds, r.task_title or ""))
    return rows
