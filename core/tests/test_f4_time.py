"""F4 time tracking (DB-backed), FR-5 verification.

Single running timer per user (DB-enforced), start/stop, manual entry,
report aggregation by project tag with billable totals and a frozen
rate snapshot, the first entry feeding the scheduler via actual_start,
and cross-org RLS isolation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from flow_core.db import admin_session, tenant_session
from flow_core.errors import DomainError
from flow_core.models.project_profile import ProjectProfile
from flow_core.services import scheduler as sch
from flow_core.services import tasks, taxonomy
from flow_core.services import time_tracking as tt
from flow_core.services.auth import signup

_RM = ZoneInfo("Europe/Rome")


def _email() -> str:
    return f"{uuid.uuid4().hex[:10]}@example.test"


async def test_single_running_timer_and_stop() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="TT")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        t1 = await tasks.create_task(s, org_id=org, actor_id=user, title="T1")
        t2 = await tasks.create_task(s, org_id=org, actor_id=user, title="T2")
        t3 = await tasks.create_task(s, org_id=org, actor_id=user, title="T3")

        # Serial timer: starting another serial one stops the previous.
        e1 = await tt.start_timer(s, org_id=org, actor_id=user, task_id=t1.id)
        assert e1.ended_at is None and e1.parallel is False
        e2 = await tt.start_timer(s, org_id=org, actor_id=user, task_id=t2.id)
        assert e2.id != e1.id
        running = await tt.running_entries(s, org_id=org, user_id=user)
        assert [r.id for r in running] == [e2.id]  # e1 auto-stopped

        # Parallel timer runs alongside the serial one.
        p3 = await tt.start_timer(
            s, org_id=org, actor_id=user, task_id=t3.id, parallel=True
        )
        assert p3.parallel is True
        running = await tt.running_entries(s, org_id=org, user_id=user)
        assert {r.id for r in running} == {e2.id, p3.id}

        # The same task can't be double-tracked simultaneously.
        with pytest.raises(DomainError):
            await tt.start_timer(
                s, org_id=org, actor_id=user, task_id=t3.id, parallel=True
            )

        # Stop a specific row by task; the other keeps running.
        s3 = await tt.stop_timer(s, org_id=org, actor_id=user, task_id=t3.id)
        assert s3.id == p3.id and s3.ended_at is not None
        running = await tt.running_entries(s, org_id=org, user_id=user)
        assert [r.id for r in running] == [e2.id]

        # No task -> stop the serial one. Then nothing running -> error.
        s2 = await tt.stop_timer(s, org_id=org, actor_id=user)
        assert s2.id == e2.id and s2.ended_at is not None
        assert await tt.running_entries(s, org_id=org, user_id=user) == []
        with pytest.raises(DomainError):
            await tt.stop_timer(s, org_id=org, actor_id=user)


async def test_report_aggregation_billable_and_rate_snapshot() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="RPT")
    org, user = a.org_id, a.user_id
    async with tenant_session(str(org), str(user)) as s:
        proj = await taxonomy.create_project(
            s, org_id=org, actor_id=user, name="Proj", tariffa=Decimal(100)
        )
        t = await tasks.create_task(s, org_id=org, actor_id=user, title="Billed", tag_ids=[proj.id])
        base = dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC)
        billed = await tt.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            started_at=base,
            duration_seconds=3600,
            billable=True,
        )
        assert billed.rate_snapshot == Decimal(100)
        await tt.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            started_at=base + dt.timedelta(hours=2),
            duration_seconds=1800,
            billable=False,
        )
        # Rate edited after the fact must not rewrite history.
        await s.execute(
            update(ProjectProfile)
            .where(ProjectProfile.tag_id == proj.id)
            .values(tariffa=Decimal(999))
        )
        rows = await tt.report(s, org_id=org, actor_id=user, group_by=tt.ReportGroup.project)
        row = next(r for r in rows if r.key == str(proj.id))
        assert row.seconds == 5400
        assert row.billable_seconds == 3600
        assert row.amount == Decimal("100.00")  # 1h * snapshot 100, not 999
        assert row.currency == "EUR"


async def test_first_entry_feeds_scheduler_actual_start() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="FEED")
    org, user = a.org_id, a.user_id
    as_of = dt.datetime(2026, 1, 12, 8, 0, tzinfo=dt.UTC)  # Mon 09:00 RM
    actual = dt.datetime(2026, 1, 13, 8, 0, tzinfo=dt.UTC)  # Tue 09:00 RM
    async with tenant_session(str(org), str(user)) as s:
        t = await tasks.create_task(
            s,
            org_id=org,
            actor_id=user,
            title="WIP",
            estimate_effort_h=Decimal(4),
            assignee_ids=[user],
        )
        await tt.add_manual_entry(
            s,
            org_id=org,
            actor_id=user,
            task_id=t.id,
            started_at=actual,
            duration_seconds=3600,
        )
        await sch.recompute(s, org_id=org, actor_id=user, as_of=as_of)
        r = await sch.get_schedule(s, org_id=org, task_id=t.id)
    assert r is not None
    # actual_start (Tue 09:00) became the ES lower bound, not Mon 09:00.
    assert r.es.astimezone(_RM).day == 13
    assert r.es.astimezone(_RM).hour == 9


async def test_time_entries_are_org_isolated() -> None:
    async with admin_session() as s:
        a = await signup(s, email=_email(), password="pw-strong-123", org_name="ISO-A")
        b = await signup(s, email=_email(), password="pw-strong-123", org_name="ISO-B")
    async with tenant_session(str(a.org_id), str(a.user_id)) as s:
        ta = await tasks.create_task(s, org_id=a.org_id, actor_id=a.user_id, title="A")
        await tt.add_manual_entry(
            s,
            org_id=a.org_id,
            actor_id=a.user_id,
            task_id=ta.id,
            started_at=dt.datetime(2026, 1, 12, 9, 0, tzinfo=dt.UTC),
            duration_seconds=600,
        )
        assert len(await tt.list_entries(s, org_id=a.org_id)) == 1
    async with tenant_session(str(b.org_id), str(b.user_id)) as s:
        assert await tt.list_entries(s, org_id=b.org_id) == []
