"""Per-day time report + the filter parity the /time view depends on.

The page draws a donut (by task/project/client) and a per-day histogram
from ONE selection, with the "By task" table underneath. They are three
reads of the same entries, so they must narrow identically and add up:
these tests pin the day bucketing (which is timezone-dependent and
therefore the easiest thing to get silently wrong), the propagation of
every filter knob into ``daily_report`` and ``task_report``, and the
per-day sums against ``report()`` itself.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.db import admin_session, tenant_session
from mycelium_core.errors import DomainError
from mycelium_core.models.task import ExecKind
from mycelium_core.models.time_entry import TimeEntry
from mycelium_core.services import memberships, tasks, taxonomy
from mycelium_core.services import time_tracking as tt
from mycelium_core.services.auth import signup

# Three consecutive UTC days, all before the 2026-03-29 DST switch, so
# Europe/Rome is a flat UTC+1 and a shifted bucket can only come from the
# conversion under test (not from an offset change mid-window).
_D1 = dt.date(2026, 3, 9)
_D2 = dt.date(2026, 3, 10)
_D3 = dt.date(2026, 3, 11)


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


def _at(day: dt.date, hour: int) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, 0, tzinfo=dt.UTC)


@dataclass(frozen=True)
class _Seed:
    """Ids the assertions key on, from ``_seed`` below."""

    client_a: uuid.UUID
    client_b: uuid.UUID
    proj_a: uuid.UUID
    proj_b: uuid.UUID
    task_a: uuid.UUID
    task_b: uuid.UUID
    task_agent: uuid.UUID


async def _seed(s: AsyncSession, org: uuid.UUID, user: uuid.UUID) -> _Seed:
    """Two clients, one project each, and time spread over three days:

    - task_a (client A / project A, human): D1 3600 billable, D2 1800 NOT
    - task_agent (client A / project A, llm_agent): D2 900 billable
    - task_b (client B / project B, human): D1 7200 billable, D3 600 billable

    So project A totals 6300 s (4500 billable) and project B 7800 s, and
    every filter under test cuts a different, non-overlapping slice.
    """
    client_a = await taxonomy.create_client(
        s,
        org_id=org,
        actor_id=user,
        name="Alpha",
        profile=taxonomy.ClientInput(legal_name="Alpha", hourly_rate=Decimal(100)),
    )
    client_b = await taxonomy.create_client(
        s,
        org_id=org,
        actor_id=user,
        name="Beta",
        profile=taxonomy.ClientInput(legal_name="Beta", hourly_rate=Decimal(50)),
    )
    proj_a = await taxonomy.create_project(
        s, org_id=org, actor_id=user, name="PA", client_tag_id=client_a.id
    )
    proj_b = await taxonomy.create_project(
        s, org_id=org, actor_id=user, name="PB", client_tag_id=client_b.id
    )
    task_a = await tasks.create_task(
        s, org_id=org, actor_id=user, title="A-work", tag_ids=[proj_a.id]
    )
    task_b = await tasks.create_task(
        s, org_id=org, actor_id=user, title="B-work", tag_ids=[proj_b.id]
    )
    # ``executor_kind`` is only persisted as passed when an assignee is
    # explicit: the auto-assign fallback realigns it with the creator's
    # identity kind (human), which would erase the llm_agent case.
    task_agent = await tasks.create_task(
        s,
        org_id=org,
        actor_id=user,
        title="A-agent",
        tag_ids=[proj_a.id],
        assignee_id=user,
        executor_kind=ExecKind.llm_agent,
    )

    async def manual(
        task_id: uuid.UUID, day: dt.date, hour: int, secs: int, billable: bool
    ) -> TimeEntry:
        return await tt.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=task_id,
            started_at=_at(day, hour),
            duration_seconds=secs,
            billable=billable,
        )

    await manual(task_a.id, _D1, 9, 3600, True)
    await manual(task_a.id, _D2, 9, 1800, False)
    agent_entry = await manual(task_agent.id, _D2, 11, 900, True)
    await manual(task_b.id, _D1, 14, 7200, True)
    await manual(task_b.id, _D3, 9, 600, True)
    # Guard the fixture itself: if the executor kind stopped being
    # snapshotted from the task, the executor_kind filter test below
    # would pass vacuously (empty result vs empty expectation).
    assert agent_entry.executor_kind is ExecKind.llm_agent

    return _Seed(
        client_a=client_a.id,
        client_b=client_b.id,
        proj_a=proj_a.id,
        proj_b=proj_b.id,
        task_a=task_a.id,
        task_b=task_b.id,
        task_agent=task_agent.id,
    )


async def test_day_bucket_follows_the_requested_timezone() -> None:
    """The same instant belongs to a different calendar day depending on
    the zone; the histogram must bucket in the zone the caller asked for,
    not in the storage zone."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="TZ")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(s, org_id=org, actor_id=user, title="Late night")
        # 23:30 UTC on D1 is 00:30 on D2 in Rome (UTC+1 in early March).
        await tt.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            started_at=dt.datetime(2026, 3, 9, 23, 30, tzinfo=dt.UTC),
            duration_seconds=1800,
        )

        async def days(tz: str | None) -> list[tuple[dt.date, int]]:
            rows = await tt.daily_report(
                s, org_id=org, actor_id=user, group_by=tt.ReportGroup.task, tz=tz
            )
            return [(r.day, r.seconds) for r in rows]

        assert await days("UTC") == [(_D1, 1800)]
        assert await days("Europe/Rome") == [(_D2, 1800)]
        # No tz == UTC (the storage zone), not the server's local zone.
        assert await days(None) == [(_D1, 1800)]


async def test_entry_is_not_split_across_midnight() -> None:
    """An entry is attributed WHOLLY to the day it STARTED on. Splitting
    it would put time on a day the ``start_from``/``start_to`` window
    (which also selects on ``started_at``) can exclude, and the histogram
    would then contradict the report next to it."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="SPAN")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(s, org_id=org, actor_id=user, title="Overnight")
        await tt.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            started_at=dt.datetime(2026, 3, 9, 22, 0, tzinfo=dt.UTC),
            duration_seconds=4 * 3600,  # runs to 02:00 on D2
        )
        rows = await tt.daily_report(
            s, org_id=org, actor_id=user, group_by=tt.ReportGroup.task, tz="UTC"
        )
        assert [(r.day, r.seconds) for r in rows] == [(_D1, 4 * 3600)]


async def test_unknown_timezone_is_rejected_not_silently_utc() -> None:
    """A typo'd zone must fail loudly: bucketing by UTC instead would
    hand the SPA a chart shifted by hours with no signal that its
    parameter was ignored."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="BADTZ")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        with pytest.raises(DomainError):
            await tt.daily_report(
                s,
                org_id=org,
                actor_id=user,
                group_by=tt.ReportGroup.project,
                tz="Mars/Olympus_Mons",
            )


async def test_daily_rows_sum_to_the_report_totals() -> None:
    """Per-group invariant: summing a bucket's per-day rows reproduces
    the ``report()`` row for the same filters, on every axis the /time
    view offers. This is what makes the histogram and the donut readable
    side by side."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="SUM")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        seed = await _seed(s, org, user)
        for group in (
            tt.ReportGroup.project,
            tt.ReportGroup.client,
            tt.ReportGroup.task,
            tt.ReportGroup.task_memo,
            tt.ReportGroup.user,
        ):
            flat = await tt.report(s, org_id=org, actor_id=user, group_by=group)
            daily = await tt.daily_report(s, org_id=org, actor_id=user, group_by=group)
            seconds: dict[str | None, int] = {}
            billable: dict[str | None, int] = {}
            amount: dict[str | None, Decimal] = {}
            labels: dict[str | None, str | None] = {}
            for r in daily:
                seconds[r.key] = seconds.get(r.key, 0) + r.seconds
                billable[r.key] = billable.get(r.key, 0) + r.billable_seconds
                amount[r.key] = amount.get(r.key, Decimal(0)) + r.amount
                labels[r.key] = r.label
            assert {r.key for r in flat} == set(seconds), group
            for r in flat:
                assert (labels[r.key], seconds[r.key], billable[r.key], amount[r.key]) == (
                    r.label,
                    r.seconds,
                    r.billable_seconds,
                    r.amount,
                ), (group, r.key)
            # Days ascending; the day's dominant bucket first within a day.
            assert daily == sorted(
                daily, key=lambda r: (r.day, -r.seconds, r.label or "", r.key or "")
            )

        # Shape: one row per (day, bucket) that actually has time, no
        # zero-filled days in between (the SPA zero-fills its own range).
        by_project = await tt.daily_report(
            s, org_id=org, actor_id=user, group_by=tt.ReportGroup.project
        )
        assert {(r.day, r.key, r.seconds) for r in by_project} == {
            (_D1, str(seed.proj_a), 3600),
            (_D1, str(seed.proj_b), 7200),
            (_D2, str(seed.proj_a), 2700),  # 1800 non-billable + 900 agent
            (_D3, str(seed.proj_b), 600),
        }
        # Money follows the same per-entry rate snapshot as ``report``:
        # only the billable 3600 s at 100/h on D1 for project A.
        d1_a = next(r for r in by_project if r.day == _D1 and r.key == str(seed.proj_a))
        assert (d1_a.billable_seconds, d1_a.amount, d1_a.currency) == (
            3600,
            Decimal("100.00"),
            "EUR",
        )


async def test_client_and_project_are_booked_once_per_entry() -> None:
    """ADR-0003: client/project are structural (one per task), so a day's
    entry is booked ONCE on those axes — the per-day split must not
    resurrect the fan-out that double-counted 1,112,725 s in production."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ONCE")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        await _seed(s, org, user)
        for group in (tt.ReportGroup.client, tt.ReportGroup.project):
            daily = await tt.daily_report(s, org_id=org, actor_id=user, group_by=group)
            per_day: dict[dt.date, int] = {}
            for r in daily:
                per_day[r.day] = per_day.get(r.day, 0) + r.seconds
            assert per_day == {_D1: 10800, _D2: 2700, _D3: 600}, group


async def test_filters_narrow_daily_and_task_report() -> None:
    """Every knob the report exposes must reach BOTH the histogram and
    the "By task" table: before this, the table ignored them and showed
    everything regardless of the selection."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="FILT")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        seed = await _seed(s, org, user)

        async def daily_secs(**kw: object) -> int:
            rows = await tt.daily_report(
                s, org_id=org, actor_id=user, group_by=tt.ReportGroup.task, **kw
            )
            return sum(r.seconds for r in rows)

        async def by_task(**kw: object) -> dict[uuid.UUID, int]:
            rows = await tt.task_report(s, org_id=org, actor_id=user, **kw)
            return {r.task_id: r.total_seconds for r in rows}

        assert await daily_secs() == 14100
        assert await by_task() == {seed.task_a: 5400, seed.task_agent: 900, seed.task_b: 7800}

        # client -> only the two tasks under client A.
        assert await daily_secs(client_tag_id=seed.client_a) == 6300
        assert await by_task(client_tag_id=seed.client_a) == {
            seed.task_a: 5400,
            seed.task_agent: 900,
        }

        # project -> only project B's task.
        assert await daily_secs(project_tag_id=seed.proj_b) == 7800
        assert await by_task(project_tag_id=seed.proj_b) == {seed.task_b: 7800}

        # billable -> drops the 1800 s non-billable entry on task_a.
        assert await daily_secs(billable=True) == 12300
        assert await by_task(billable=True) == {
            seed.task_a: 3600,
            seed.task_agent: 900,
            seed.task_b: 7800,
        }
        assert await by_task(billable=False) == {seed.task_a: 1800}

        # executor kind -> only the agent-executed entry.
        assert await daily_secs(executor_kind=ExecKind.llm_agent) == 900
        assert await by_task(executor_kind=ExecKind.llm_agent) == {seed.task_agent: 900}
        assert await by_task(executor_kind=ExecKind.human) == {
            seed.task_a: 5400,
            seed.task_b: 7800,
        }

        # window -> started_at based, same as ``report``/``list_entries``.
        assert await daily_secs(start_from=_at(_D2, 0)) == 3300
        assert await by_task(start_from=_at(_D2, 0), start_to=_at(_D3, 0)) == {
            seed.task_a: 1800,
            seed.task_agent: 900,
        }

        # Filters compose (client A + billable only): 3600 + 900.
        assert await daily_secs(client_tag_id=seed.client_a, billable=True) == 4500
        assert await by_task(client_tag_id=seed.client_a, billable=True) == {
            seed.task_a: 3600,
            seed.task_agent: 900,
        }

        # A tag nothing is tagged with yields no rows — emphatically NOT
        # the unfiltered set, which is the failure mode the /time view
        # showed before ("always see everything").
        orphan = uuid.uuid4()
        assert await daily_secs(project_tag_id=orphan) == 0
        assert await by_task(project_tag_id=orphan) == {}
        assert await daily_secs(client_tag_id=orphan) == 0
        assert await by_task(client_tag_id=orphan) == {}


async def test_running_entries_are_excluded() -> None:
    """A live timer has no ``duration_seconds`` yet: it must not appear
    in the histogram (nor create a phantom bucket for today) until it is
    stopped."""
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="RUN")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        seed = await _seed(s, org, user)
        before = await tt.daily_report(
            s, org_id=org, actor_id=user, group_by=tt.ReportGroup.project
        )
        await tt.start_timer(s, org_id=org, actor_id=user, task_id=seed.task_a)
        after = await tt.daily_report(s, org_id=org, actor_id=user, group_by=tt.ReportGroup.project)
        assert after == before
        # Same for the table: the live timer adds no seconds and no row.
        assert {
            r.task_id: r.total_seconds for r in await tt.task_report(s, org_id=org, actor_id=user)
        } == {
            seed.task_a: 5400,
            seed.task_agent: 900,
            seed.task_b: 7800,
        }


async def test_task_report_is_org_wide_unless_scoped_to_a_user() -> None:
    """Deliberate change: ``task_report`` used to hardwire the acting
    user, making the "By task" table the only panel on the page scoped to
    the caller while ``report``/``list_entries`` beside it are org-wide.
    The default is now org-wide and ``user_id`` is an explicit filter,
    mirroring ``GET /time/entries``."""
    async with admin_session() as s:
        owner = await signup(s, email=_email(), password="pw-strong-123", org_name="ORGWIDE")
        mate_email = _email()
        mate = await signup(s, email=mate_email, password="pw-strong-123", org_name="MATE-OWN")
    org, user = owner.org_id, owner.user_id
    async with tenant_session(str(org), str(user)) as s:
        await memberships.add_member(s, org_id=org, actor_id=user, email=mate_email, role="member")
        t_own = await tasks.create_task(s, org_id=org, actor_id=user, title="Mine")
        t_mate = await tasks.create_task(s, org_id=org, actor_id=user, title="Theirs")
        await tt.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=t_own.id,
            started_at=_at(_D1, 9),
            duration_seconds=3600,
        )
    async with tenant_session(str(org), str(mate.user_id)) as s:
        await tt.add_manual_entry(
            s,
            org_id=org,
            actor_id=mate.user_id,
            task_id=t_mate.id,
            started_at=_at(_D1, 10),
            duration_seconds=1800,
        )
    async with tenant_session(str(org), str(user)) as s:
        org_wide = {
            r.task_id: r.total_seconds for r in await tt.task_report(s, org_id=org, actor_id=user)
        }
        assert org_wide == {t_own.id: 3600, t_mate.id: 1800}
        mine = {
            r.task_id: r.total_seconds
            for r in await tt.task_report(s, org_id=org, actor_id=user, user_id=user)
        }
        assert mine == {t_own.id: 3600}
        # And the report above the table sees the same org-wide total.
        by_task = await tt.report(s, org_id=org, actor_id=user, group_by=tt.ReportGroup.task)
        assert sum(r.seconds for r in by_task) == sum(org_wide.values())
